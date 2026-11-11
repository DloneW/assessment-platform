"""
Views are thin: pick the object through the authorization layer, call a workflow service,
render. Who may do what is decided only by `authorization.services`.

Response policy (same as Day 2): anonymous -> login; object not permitted -> 404 identical to
missing; a permitted manager attempting a state-invalid action (editing a published test) ->
redirect back with a message; tampered answer ids -> 400.
"""

import hashlib
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.models import Course
from authorization import services as authz
from authorization.enforcement import get_authorized_or_404

from . import services, uploads
from .forms import QuestionForm, TestForm
from .forms import AssessmentImportForm
from .importers import ImportFormatError, MAX_IMPORT_QUESTIONS, parse_upload
from .models import AnswerFile, Attempt, Choice, ImportHistory, Question, QuestionBankQuestion, StudentAnswer, Test

FORM_METHODS = ["GET", "HEAD", "POST"]
MAX_BANK_SELECTIONS = 500


def _test_or_404(request, check, **lookup):
    return get_authorized_or_404(request.user, check, Test.objects.select_related("course"), **lookup)


def _question_or_404(test, pk):
    try:
        return test.questions.get(pk=pk)  # scoped to the test in the URL
    except Question.DoesNotExist:
        raise Http404 from None


# --- Lecturer: authoring ------------------------------------------------------------------

@require_http_methods(FORM_METHODS)
@login_required
def create_test(request, course_pk):
    course = get_authorized_or_404(
        request.user, authz.can_create_test, Course.objects.select_related("department", "level"), pk=course_pk
    )
    form = TestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        # course and creator come from the URL object and the session, never from the form.
        test = services.create_test(course=course, creator=request.user, cleaned=form.cleaned_data)
        messages.success(request, "Draft created. Now add questions.")
        return redirect("assessments:test_manage", pk=test.pk)
    return render(request, "assessments/test_form.html", {"form": form, "course": course, "test": None})


def _import_history(course, creator, file_name, file_hash):
    try:
        return ImportHistory.objects.create(
            course=course, creator=creator, file_name=file_name[:255], file_hash=file_hash
        )
    except IntegrityError:
        return None


def _decorate_import_rows(rows, course):
    """Attach row errors and stable A-E input fields to editable preview data."""
    seen = set()
    existing = {
        services.normalize_question_text(text)
        for text in (
            *Question.objects.filter(test__course=course).values_list("text", flat=True),
            *QuestionBankQuestion.objects.filter(course=course).values_list("text", flat=True),
        )
    }
    decorated = []
    for row in rows:
        row = dict(row)
        row.setdefault("errors", [])
        row["errors"] = list(row["errors"])
        text = services.normalize_question_text(row.get("text", ""))
        if text and text in seen:
            row["errors"].append("Duplicate question in this upload.")
        if text and text in existing:
            row["errors"].append("This question already exists in this course.")
        if text:
            seen.add(text)
        row["choice_fields"] = [
            {"letter": chr(65 + index), "value": row.get("choices", [])[index] if index < len(row.get("choices", [])) else ""}
            for index in range(5)
        ]
        decorated.append(row)
    return decorated


def _posted_import_rows(request, course):
    raw_count = request.POST.get("question_count", "")
    if not re.fullmatch(r"[0-9]{1,3}", raw_count):
        return [], ["The preview is invalid. Upload the file again."]
    count = int(raw_count)
    if not 1 <= count <= MAX_IMPORT_QUESTIONS:
        return [], [f"Select at least one question and no more than {MAX_IMPORT_QUESTIONS}."]
    rows = []
    errors = []
    for index in range(count):
        if request.POST.get(f"delete_{index}") == "on":
            continue
        text = request.POST.get(f"question_{index}_text", "").strip()
        category = request.POST.get(f"question_{index}_category", "").strip()
        question_type = request.POST.get(f"question_{index}_type", "")
        raw_marks = request.POST.get(f"question_{index}_marks", "")
        if not re.fullmatch(r"[0-9]{1,3}", raw_marks):
            marks = 0
        else:
            marks = int(raw_marks)
        source_choices = [request.POST.get(f"question_{index}_choice_{letter}", "").strip() for letter in "ABCDE"]
        correct_raw = request.POST.get(f"question_{index}_correct", "")
        if not re.fullmatch(r"[1-5]", correct_raw):
            correct_source = 0
        else:
            correct_source = int(correct_raw)
        selected_choices = [(position, value) for position, value in enumerate(source_choices, 1) if value]
        compact_choices = [value for _, value in selected_choices]
        correct = next(
            (position for position, (source_position, _) in enumerate(selected_choices, 1) if source_position == correct_source),
            0,
        )
        row_errors = []
        if not text:
            row_errors.append("Question text is required.")
        if len(text) > 2000:
            row_errors.append("Question text cannot exceed 2,000 characters.")
        if len(category) > 100:
            row_errors.append("Category cannot exceed 100 characters.")
        if not 2 <= len(compact_choices) <= 5:
            row_errors.append("Provide 2 to 5 non-empty answer options.")
        if any(len(choice) > 500 for choice in compact_choices):
            row_errors.append("Answer options cannot exceed 500 characters.")
        if len({choice.casefold() for choice in compact_choices}) != len(compact_choices):
            row_errors.append("Answer options must be different.")
        if not 1 <= marks <= 100:
            row_errors.append("Marks must be between 1 and 100.")
        if not 1 <= correct <= len(compact_choices):
            row_errors.append("Select a correct answer that has an option.")
        if question_type not in Question.Type.values:
            row_errors.append("Select a supported question type.")
        elif question_type == Question.Type.TRUE_FALSE and len(compact_choices) != 2:
            row_errors.append("True / false questions need exactly two options.")
        rows.append(
            {
                "text": text,
                "choices": compact_choices,
                "correct": correct,
                "marks": marks,
                "category": category,
                "question_type": question_type,
                "errors": row_errors,
            }
        )
    rows = _decorate_import_rows(rows, course)
    for index, row in enumerate(rows, start=1):
        if row["errors"]:
            errors.extend(f"Question {index}: {message}" for message in row["errors"])
    if not rows:
        errors.append("Select at least one question to import.")
    return rows, errors


@require_http_methods(FORM_METHODS)
@login_required
def import_assessment(request, course_pk):
    course = get_authorized_or_404(
        request.user, authz.can_create_test, Course.objects.select_related("department", "level"), pk=course_pk
    )
    if request.method == "GET":
        return render(
            request,
            "assessments/import_assessment.html",
            {"form": AssessmentImportForm(), "course": course},
        )

    if request.POST.get("action") == "save":
        form = TestForm(request.POST)
        import_id = request.POST.get("import_id", "")
        history = ImportHistory.objects.filter(
            pk=int(import_id) if re.fullmatch(r"[0-9]{1,18}", import_id) else None,
            course=course,
            creator=request.user,
            status=ImportHistory.Status.PREVIEWED,
        ).first()
        if history is None:
            form.add_error(None, "This import preview has expired. Upload the file again.")
            return render(request, "assessments/import_assessment.html", {"form": form, "course": course})
        rows, row_errors = _posted_import_rows(request, course)
        if not form.is_valid() or row_errors:
            history.details = "\n".join(row_errors)[:5000]
            history.save(update_fields=["details"])
            return render(
                request,
                "assessments/import_preview.html",
                {"form": form, "course": course, "history": history, "questions": rows, "row_errors": row_errors},
            )
        try:
            test = services.save_imported_assessment(
                course=course,
                creator=request.user,
                cleaned=form.cleaned_data,
                questions=rows,
                history_id=history.pk,
            )
        except (services.ImportConflict, ImportHistory.DoesNotExist) as exc:
            form.add_error(None, str(exc) or "This import preview is no longer available.")
            return render(
                request,
                "assessments/import_preview.html",
                {"form": form, "course": course, "history": history, "questions": rows, "row_errors": [str(exc)]},
            )
        messages.success(request, f"Imported {len(rows)} questions into a new draft assessment.")
        return redirect("assessments:test_manage", pk=test.pk)

    form = AssessmentImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, "assessments/import_assessment.html", {"form": form, "course": course})
    upload = form.cleaned_data["file"]
    raw = upload.read()
    upload.seek(0)
    file_hash = hashlib.sha256(raw).hexdigest()
    history = ImportHistory.objects.filter(course=course, creator=request.user, file_hash=file_hash).first()
    if history is not None and history.status != ImportHistory.Status.PREVIEWED:
        form.add_error("file", "This file has already been processed or has an active preview.")
        return render(request, "assessments/import_assessment.html", {"form": form, "course": course})
    if history is None:
        history = _import_history(course, request.user, upload.name, file_hash)
        if history is None:
            history = ImportHistory.objects.filter(course=course, creator=request.user, file_hash=file_hash).first()
        if history is None or history.status != ImportHistory.Status.PREVIEWED:
            form.add_error("file", "This file has already been processed.")
            return render(request, "assessments/import_assessment.html", {"form": form, "course": course})
    elif history.file_name != upload.name[:255]:
        history.file_name = upload.name[:255]
        history.save(update_fields=["file_name"])
    try:
        questions = parse_upload(upload)
    except ImportFormatError as exc:
        ImportHistory.objects.filter(pk=history.pk, status=ImportHistory.Status.PREVIEWED).update(
            status=ImportHistory.Status.FAILED, details=str(exc)
        )
        form.add_error("file", str(exc))
        return render(
            request,
            "assessments/import_assessment.html",
            {"form": form, "course": course, "history": history},
        )
    decorated = _decorate_import_rows(questions, course)
    details = "\n".join(
        f"Question {index}: {message}"
        for index, question in enumerate(decorated, start=1)
        for message in question["errors"]
    )[:5000]
    updated = ImportHistory.objects.filter(pk=history.pk, status=ImportHistory.Status.PREVIEWED).update(
        question_count=len(decorated), details=details
    )
    if not updated:
        form.add_error("file", "This import has already been processed.")
        return render(request, "assessments/import_assessment.html", {"form": form, "course": course})
    history.question_count = len(decorated)
    history.details = details
    return render(
        request,
        "assessments/import_preview.html",
        {
            "form": form,
            "course": course,
            "history": history,
            "questions": decorated,
            "row_errors": [],
        },
    )


@require_safe
@login_required
def import_template(request):
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Questions"
    sheet.append(["Question", "Question Type", "A", "B", "C", "D", "E", "Correct Answer", "Marks", "Category"])
    sheet.append(["What is 2 + 2?", "MULTIPLE_CHOICE", "3", "4", "5", "", "", "B", 1, "Example"])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:J2"
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="assessment-question-template.xlsx"'
    return response


@require_http_methods(["GET", "POST"])
@login_required
def question_bank(request, course_pk):
    course = get_authorized_or_404(request.user, authz.can_manage_course, Course.objects.all(), pk=course_pk)
    draft_tests = course.tests.filter(status=Test.Status.DRAFT).order_by("title")
    if request.method == "POST":
        raw_ids = request.POST.getlist("question_ids")
        if len(raw_ids) > MAX_BANK_SELECTIONS or any(not re.fullmatch(r"[0-9]{1,18}", value) for value in raw_ids):
            messages.error(request, f"Select up to {MAX_BANK_SELECTIONS} valid bank questions.")
            return redirect("assessments:question_bank", course_pk=course.pk)
        test = get_authorized_or_404(
            request.user,
            authz.can_edit_test,
            Test.objects.select_related("course"),
            pk=int(request.POST["test_id"])
            if re.fullmatch(r"[0-9]{1,18}", request.POST.get("test_id", ""))
            else None,
        )
        if test.course_id != course.pk:
            raise Http404
        try:
            added = services.add_bank_questions(test, [int(value) for value in raw_ids])
        except services.ImportConflict as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Added {added} question{'s' if added != 1 else ''} to {test.title}.")
            return redirect("assessments:test_manage", pk=test.pk)
    questions = QuestionBankQuestion.objects.filter(course=course)
    search = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    if search:
        questions = questions.filter(text__icontains=search)
    if category:
        questions = questions.filter(category=category)
    return render(
        request,
        "assessments/question_bank.html",
        {
            "course": course,
            "questions": questions,
            "search": search,
            "selected_category": category,
            "categories": QuestionBankQuestion.objects.filter(course=course)
            .exclude(category="")
            .order_by("category")
            .values_list("category", flat=True)
            .distinct(),
            "draft_tests": draft_tests,
            "history": ImportHistory.objects.filter(course=course)[:20],
        },
    )


@require_safe
@login_required
def test_manage(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    questions = test.questions.prefetch_related("choices")
    problems = services.validate_publishable(test) if test.is_draft else []
    return render(
        request,
        "assessments/test_manage.html",
        {
            "test": test,
            "questions": questions,
            "problems": problems,
            "can_edit": authz.can_edit_test(request.user, test),
            "can_publish": authz.can_publish_test(request.user, test),
            "can_archive": authz.can_archive_test(request.user, test),
        },
    )


@require_http_methods(FORM_METHODS)
@login_required
def test_edit(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    if not authz.can_edit_test(request.user, test):
        messages.error(request, "Only draft tests can be edited.")
        return redirect("assessments:test_manage", pk=test.pk)
    form = TestForm(request.POST) if request.method == "POST" else TestForm.from_test(test)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_test(test, form.cleaned_data)
        except services.StateError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Test updated.")
        return redirect("assessments:test_manage", pk=test.pk)
    return render(request, "assessments/test_form.html", {"form": form, "course": test.course, "test": test})


@require_http_methods(FORM_METHODS)
@login_required
def question_add(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    if not authz.can_edit_test(request.user, test):
        messages.error(request, "Only draft tests can be edited.")
        return redirect("assessments:test_manage", pk=test.pk)
    form = QuestionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.create_question(test, form.cleaned_data)
        except services.StateError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Question added.")
        return redirect("assessments:test_manage", pk=test.pk)
    return render(request, "assessments/question_form.html", {"form": form, "test": test, "question": None})


@require_http_methods(FORM_METHODS)
@login_required
def question_edit(request, pk, question_pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    question = _question_or_404(test, question_pk)
    if not authz.can_edit_test(request.user, test):
        messages.error(request, "Only draft tests can be edited.")
        return redirect("assessments:test_manage", pk=test.pk)
    form = QuestionForm(request.POST) if request.method == "POST" else QuestionForm.from_question(question)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_question(question, form.cleaned_data)
        except services.StateError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Question updated.")
        return redirect("assessments:test_manage", pk=test.pk)
    return render(request, "assessments/question_form.html", {"form": form, "test": test, "question": question})


@require_POST
@login_required
def question_delete(request, pk, question_pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    question = _question_or_404(test, question_pk)
    if not authz.can_edit_test(request.user, test):
        messages.error(request, "Only draft tests can be edited.")
    else:
        try:
            services.delete_question(question)
            messages.success(request, "Question deleted.")
        except services.StateError as exc:
            messages.error(request, str(exc))
    return redirect("assessments:test_manage", pk=test.pk)


@require_POST
@login_required
def test_publish(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    if not authz.can_publish_test(request.user, test):
        messages.error(request, "This test cannot be published.")
        return redirect("assessments:test_manage", pk=test.pk)
    try:
        services.publish_test(test)
    except services.PublishError as exc:
        for problem in exc.problems:
            messages.error(request, problem)
    except services.StateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Test published. Questions are now locked.")
    return redirect("assessments:test_manage", pk=test.pk)


@require_POST
@login_required
def test_archive(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    if not authz.can_archive_test(request.user, test):
        messages.error(request, "Only a published test can be archived.")
    else:
        try:
            services.archive_test(test)
            messages.success(request, "Test archived.")
        except services.StateError as exc:
            messages.error(request, str(exc))
    return redirect("assessments:test_manage", pk=test.pk)


# --- Student: taking a test -----------------------------------------------------------------

def _own_attempt_or_404(request, test_pk, pk):
    """The attempt must belong to this student AND to the test named in the URL."""
    return get_authorized_or_404(
        request.user,
        authz.can_view_attempt,
        Attempt.objects.select_related("test", "test__course"),
        pk=pk,
        test_id=test_pk,
    )


@require_safe
@login_required
def test_intro(request, pk):
    test = _test_or_404(request, authz.can_take_test, pk=pk)
    attempt = Attempt.objects.filter(student=request.user.student_profile, test=test).first()
    return render(
        request,
        "assessments/test_intro.html",
        {
            "test": test,
            "attempt": attempt,
            "can_start": authz.can_start_attempt(request.user, test),
            "question_count": test.questions.count(),
        },
    )


@require_POST
@login_required
def attempt_start(request, pk):
    test = _test_or_404(request, authz.can_take_test, pk=pk)
    student = request.user.student_profile
    existing = Attempt.objects.filter(student=student, test=test).first()
    if existing is None and not authz.can_start_attempt(request.user, test):
        messages.error(request, "This test is not open right now.")
        return redirect("assessments:test_intro", pk=test.pk)
    try:
        attempt, _created = services.start_attempt(student, test)
    except services.AttemptFinalized as done:
        # Already submitted or expired: show the result; never re-enter the test.
        messages.error(request, str(done))
        return redirect("assessments:attempt_detail", test_pk=test.pk, pk=done.attempt.pk)
    except services.StateError as exc:
        messages.error(request, str(exc))
        return redirect("assessments:test_intro", pk=test.pk)
    return redirect("assessments:attempt_detail", test_pk=test.pk, pk=attempt.pk)


@require_safe
@login_required
def attempt_detail(request, test_pk, pk):
    attempt = _own_attempt_or_404(request, test_pk, pk)
    attempt = services.expire_if_overdue(attempt)
    attempt = Attempt.objects.select_related("test", "test__course").get(pk=attempt.pk)
    if not attempt.is_in_progress:
        answers = (
            StudentAnswer.objects.filter(attempt=attempt)
            .select_related("question", "choice", "file")
            .defer("question__marking_guide", "choice__is_correct")
        )
        by_question = {a.question_id: a for a in answers}
        rows = [
            {"question": q, "answer": by_question.get(q.pk)}
            for q in Question.objects.filter(test_id=attempt.test_id).defer("marking_guide")
        ]
        return render(
            request,
            "assessments/attempt_result.html",
            {"attempt": attempt, "rows": rows, "pending": services.pending_grading_count(attempt)},
        )

    # Never load the answer key or marking guides into student-facing code.
    questions = (
        Question.objects.filter(test_id=attempt.test_id)
        .defer("marking_guide")
        .prefetch_related(Prefetch("choices", queryset=Choice.objects.defer("is_correct")))
    )
    answers = {a.question_id: a for a in StudentAnswer.objects.filter(attempt=attempt).select_related("file")}
    selected = {qid: a.choice_id for qid, a in answers.items() if a.choice_id}
    texts = {qid: a.text for qid, a in answers.items() if a.text}
    files = {qid: a.file for qid, a in answers.items() if getattr(a, "file", None)}
    remaining = max(0, int((attempt.deadline - timezone.now()).total_seconds()))
    return render(
        request,
        "assessments/attempt_take.html",
        {
            "attempt": attempt,
            "questions": questions,
            "selected": selected,
            "texts": texts,
            "files": files,
            "remaining_seconds": remaining,
            "allowed_types": uploads.ALLOWED_DESCRIPTION,
            "max_upload_mb": uploads.MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


def _store_uploaded_files(request, attempt):
    """Save every `f_<question_id>` file in the request. Returns a list of error strings.
    A closed attempt is not an error here: the caller settles state."""
    errors = []
    for key, uploaded in request.FILES.items():
        match = re.fullmatch(r"f_([0-9]{1,18})", key)
        if match is None:
            raise services.InvalidAnswer("Malformed upload.")
        try:
            services.save_file_answer(attempt, int(match.group(1)), uploaded)
        except uploads.UploadRejected as exc:
            errors.append(str(exc))
    return errors


@require_POST
@login_required
def attempt_save(request, test_pk, pk):
    attempt = _own_attempt_or_404(request, test_pk, pk)
    ajax = request.POST.get("ajax") == "1"
    try:
        selections = services.parse_selections(request.POST)
        texts = services.parse_texts(request.POST)
        if authz.can_answer_attempt(request.user, attempt):
            services.record_answers(attempt, selections, texts)
            upload_errors = _store_uploaded_files(request, attempt)
            if upload_errors:
                for error in upload_errors:
                    messages.error(request, error)
                return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)
        else:
            attempt = services.expire_if_overdue(attempt)
            if ajax:
                return HttpResponse(status=409)
            messages.error(request, "This attempt is closed; your answers were not saved.")
            return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)
    except services.InvalidAnswer:
        return HttpResponseBadRequest("Invalid answer.")
    except services.StateError as exc:
        if ajax:
            return HttpResponse(status=409)
        messages.error(request, str(exc))
        return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)
    if ajax:
        return HttpResponse(status=204)
    messages.success(request, "Progress saved.")
    return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)


@require_POST
@login_required
def attempt_submit(request, test_pk, pk):
    attempt = _own_attempt_or_404(request, test_pk, pk)
    try:
        selections = services.parse_selections(request.POST)
        texts = services.parse_texts(request.POST)
    except services.InvalidAnswer:
        return HttpResponseBadRequest("Invalid answer.")
    if authz.can_submit_attempt(request.user, attempt):
        try:
            # Answers sent with the submit count only if they arrive before the deadline.
            if authz.can_answer_attempt(request.user, attempt):
                services.record_answers(attempt, selections, texts)
                upload_errors = _store_uploaded_files(request, attempt)
                if upload_errors:  # do not submit with a rejected file; let the student fix it
                    for error in upload_errors:
                        messages.error(request, error)
                    return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)
        except services.InvalidAnswer:
            return HttpResponseBadRequest("Invalid answer.")
        except services.StateError:
            pass  # closed in the meantime; submit_attempt below settles the final state
        final = services.submit_attempt(attempt)
        if final.status == Attempt.Status.EXPIRED:
            messages.error(request, "Time ran out, so the attempt was closed with the answers saved in time.")
        else:
            messages.success(request, "Test submitted.")
    # Already finished: a repeated submit changes nothing and just shows the result.
    return redirect("assessments:attempt_detail", test_pk=test_pk, pk=pk)


# --- Marking and downloads ----------------------------------------------------------------

@require_safe
@login_required
def submissions(request, pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    services.expire_overdue_for_test(test)
    attempts = Attempt.objects.filter(test=test).select_related("student__user").order_by("student__student_id")
    rows = [{"attempt": a, "pending": services.pending_grading_count(a)} for a in attempts]
    return render(request, "assessments/submissions.html", {"test": test, "rows": rows})


@require_http_methods(FORM_METHODS)
@login_required
def grade(request, pk, attempt_pk):
    test = _test_or_404(request, authz.can_manage_test, pk=pk)
    attempt = get_authorized_or_404(
        request.user,
        authz.can_grade_attempt,
        Attempt.objects.select_related("test", "test__course", "student__user"),
        pk=attempt_pk,
        test_id=test.pk,
    )
    manual = list(
        StudentAnswer.objects.filter(attempt=attempt, question__kind__in=services.MANUAL_KINDS)
        .select_related("question", "file")
        .order_by("question__order", "question__pk")
    )
    answered = {a.question_id: a for a in manual}
    questions = list(Question.objects.filter(test_id=test.pk, kind__in=services.MANUAL_KINDS))
    rows = [{"question": q, "answer": answered.get(q.pk)} for q in questions]
    if request.method == "POST":
        try:
            grades = services.parse_grades(request.POST, manual)
            services.grade_attempt(attempt, grades, request.user)
        except (services.InvalidAnswer, services.StateError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Marks saved.")
            return redirect("assessments:grade", pk=test.pk, attempt_pk=attempt.pk)
        return redirect("assessments:grade", pk=test.pk, attempt_pk=attempt.pk)
    attempt = Attempt.objects.select_related("student__user").get(pk=attempt.pk)
    return render(request, "assessments/grade.html", {"test": test, "attempt": attempt, "rows": rows})


@require_safe
@login_required
def answer_file(request, test_pk, pk, answer_pk):
    """Serve an uploaded answer file as a download, to its owner or a manager of the course."""
    attempt = get_authorized_or_404(
        request.user,
        authz.can_download_answer_file,
        Attempt.objects.select_related("test", "test__course"),
        pk=pk,
        test_id=test_pk,
    )
    try:
        stored = AnswerFile.objects.get(answer_id=answer_pk, answer__attempt_id=attempt.pk)
    except AnswerFile.DoesNotExist:
        raise Http404 from None
    response = HttpResponse(bytes(stored.content), content_type=stored.content_type)
    response["Content-Disposition"] = content_disposition_header(True, stored.filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response
