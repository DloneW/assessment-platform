"""
Views are thin: pick the object through the authorization layer, call a workflow service,
render. Who may do what is decided only by `authorization.services`.

Response policy (same as Day 2): anonymous -> login; object not permitted -> 404 identical to
missing; a permitted manager attempting a state-invalid action (editing a published test) ->
redirect back with a message; tampered answer ids -> 400.
"""

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
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
from .models import AnswerFile, Attempt, Choice, Question, StudentAnswer, Test

FORM_METHODS = ["GET", "HEAD", "POST"]


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
