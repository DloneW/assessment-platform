"""
Assessment workflow. Views call these functions; these functions never decide WHO may act
(that is `authorization.services`) and never trust the client for anything that matters.

Server-side truth:
  * score / max_score are computed here from stored answers and the answer key, never read
    from a request;
  * the deadline is fixed when the attempt starts (started_at + duration) and compared with
    the server clock on every answer and submission;
  * state changes (start, finalize, publish) are compare-and-set inside transactions, so a
    double submit or a concurrent publish can only succeed once.

Expiry is lazy: an overdue attempt is finalized as EXPIRED the next time anything touches it
(there is no background worker). Nothing can write to an overdue attempt in the meantime
because every write path re-checks the clock.
"""

import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import uploads
from .models import (
    MAX_DURATION_MINUTES,
    MAX_FEEDBACK_CHARS,
    MAX_SHORT_ANSWER_CHARS,
    AnswerFile,
    Attempt,
    Choice,
    Question,
    StudentAnswer,
    Test,
)

MAX_SELECTIONS_PER_REQUEST = 500
# ASCII digits only and a bounded length: int() would also accept '1_0', '+1', ' 1' and non-ASCII digits.
_ANSWER_KEY = re.compile(r"q_([0-9]{1,18})")
_ID_VALUE = re.compile(r"[0-9]{1,18}")
_TEXT_KEY = re.compile(r"t_([0-9]{1,18})")


class StateError(Exception):
    """The action is not valid in the object's current state (user-facing message)."""


class InvalidAnswer(Exception):
    """The submitted question/choice ids do not form a valid answer for this attempt."""


class PublishError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- Authoring -------------------------------------------------------------------------

def _locked_draft(test_id):
    """Re-read the test inside the transaction; refuse unless it is still a draft."""
    test = Test.objects.select_for_update().get(pk=test_id)
    if not test.is_draft:
        raise StateError("This test is no longer a draft, so it cannot be changed.")
    return test


def create_test(*, course, creator, cleaned):
    """`course` and `creator` come from the server (URL object + session user), never the form."""
    return Test.objects.create(
        course=course,
        creator=creator,
        title=cleaned["title"],
        description=cleaned.get("description", ""),
        duration_minutes=cleaned["duration_minutes"],
        opens_at=cleaned.get("opens_at"),
        closes_at=cleaned.get("closes_at"),
    )


def update_test(test, cleaned):
    with transaction.atomic():
        locked = _locked_draft(test.pk)
        for field in ("title", "description", "duration_minutes", "opens_at", "closes_at"):
            setattr(locked, field, cleaned.get(field))
        locked.description = locked.description or ""
        locked.save()
    return locked


def _write_choices(question, choices, correct_index):
    for position, text in enumerate(choices, start=1):
        Choice.objects.create(question=question, text=text, order=position, is_correct=(position == correct_index))


def create_question(test, cleaned):
    """`cleaned` comes from QuestionForm: kind, text, marks, marking_guide and, for multiple choice,
    choices (list of strings) and correct (1-based). Short-answer and file questions have no choices."""
    kind = cleaned.get("kind", Question.Kind.MCQ)
    try:
        with transaction.atomic():
            locked = _locked_draft(test.pk)
            highest = max(locked.questions.values_list("order", flat=True), default=0)
            question = Question.objects.create(
                test=locked,
                kind=kind,
                text=cleaned["text"],
                marks=cleaned["marks"],
                marking_guide=cleaned.get("marking_guide", "") if kind != Question.Kind.MCQ else "",
                order=highest + 1,
            )
            if kind == Question.Kind.MCQ:
                _write_choices(question, cleaned["choices"], cleaned["correct"])
    except IntegrityError:
        raise StateError("Someone else changed this test at the same time. Please try again.") from None
    return question


def update_question(question, cleaned):
    with transaction.atomic():
        _locked_draft(question.test_id)
        question = Question.objects.get(pk=question.pk)
        kind = cleaned.get("kind", Question.Kind.MCQ)
        question.kind = kind
        question.text = cleaned["text"]
        question.marks = cleaned["marks"]
        question.marking_guide = cleaned.get("marking_guide", "") if kind != Question.Kind.MCQ else ""
        question.save()
        # Drafts have no attempts, so no answers can reference these choices.
        question.choices.all().delete()
        if kind == Question.Kind.MCQ:
            _write_choices(question, cleaned["choices"], cleaned["correct"])
    return question


def delete_question(question):
    with transaction.atomic():
        _locked_draft(question.test_id)
        Question.objects.filter(pk=question.pk).delete()


def validate_publishable(test, now=None):
    """Every reason this test cannot be published yet (empty list = publishable)."""
    now = now or timezone.now()
    problems = []
    if not 1 <= test.duration_minutes <= MAX_DURATION_MINUTES:
        problems.append(f"Duration must be between 1 and {MAX_DURATION_MINUTES} minutes.")
    if test.opens_at and test.closes_at and test.closes_at <= test.opens_at:
        problems.append("The closing time must be after the opening time.")
    if test.closes_at and test.closes_at <= now:
        problems.append("The closing time is already in the past.")
    questions = list(
        Question.objects.filter(test=test)
        .annotate(n_choices=Count("choices"), n_correct=Count("choices", filter=Q(choices__is_correct=True)))
        .order_by("order")
    )
    if not questions:
        problems.append("Add at least one question.")
    for number, question in enumerate(questions, start=1):
        if question.kind == Question.Kind.MCQ:
            if question.n_choices < 2:
                problems.append(f"Question {number} needs at least two choices.")
            if question.n_correct != 1:
                problems.append(f"Question {number} must have exactly one correct choice.")
        elif question.n_choices:
            problems.append(f"Question {number} is not multiple choice, so it cannot have choices.")
        if question.marks < 1:
            problems.append(f"Question {number} must be worth at least 1 mark.")
    return problems


def publish_test(test, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        locked = _locked_draft(test.pk)
        problems = validate_publishable(locked, now)
        if problems:
            raise PublishError(problems)
        changed = Test.objects.filter(pk=locked.pk, status=Test.Status.DRAFT).update(
            status=Test.Status.PUBLISHED, published_at=now, updated_at=now
        )
        if not changed:
            raise StateError("This test was already published.")
    return Test.objects.get(pk=test.pk)


def archive_test(test, now=None):
    """PUBLISHED -> ARCHIVED, and only that transition.

    The row is re-read and locked inside the transaction (a no-op lock on SQLite, which
    serializes writers; a row lock elsewhere), the state is checked on that fresh row, and the
    write is a compare-and-set, so a stale object or a concurrent change can never produce a
    DRAFT -> ARCHIVED or ARCHIVED -> ARCHIVED transition. The new status is a server constant.
    """
    now = now or timezone.now()
    with transaction.atomic():
        current = Test.objects.select_for_update().get(pk=test.pk)
        if not current.is_published:
            raise StateError("Only a published test can be archived.")
        changed = Test.objects.filter(pk=current.pk, status=Test.Status.PUBLISHED).update(
            status=Test.Status.ARCHIVED, updated_at=now
        )
        if not changed:
            raise StateError("Only a published test can be archived.")
    return Test.objects.get(pk=test.pk)


# --- Taking a test ----------------------------------------------------------------------

class AttemptFinalized(StateError):
    """The student already has a SUBMITTED or EXPIRED attempt; it is shown, never reopened."""

    def __init__(self, attempt):
        super().__init__("You have already completed this test.")
        self.attempt = attempt


def start_attempt(student, test, now=None):
    """State-aware start. Returns (attempt, created).

        no attempt            -> create an IN_PROGRESS attempt        (created=True)
        IN_PROGRESS attempt   -> resume it                            (created=False)
        SUBMITTED / EXPIRED   -> raise AttemptFinalized(attempt); nothing is created or changed

    An in-progress attempt that is already past its deadline is expired first, so it cannot be
    "resumed" by starting again. The unique (student, test) constraint still backs the
    one-attempt policy: get_or_create turns a lost race into a plain "get", and a finalized
    row found that way is refused like any other. The caller has checked can_take_test and,
    for a first attempt, can_start_attempt.
    """
    now = now or timezone.now()
    # Decide inside the transaction, but raise AFTER it commits: raising inside would roll back
    # the lazy expiry done below, leaving an overdue attempt still IN_PROGRESS.
    with transaction.atomic():
        fresh = Test.objects.get(pk=test.pk)
        existing = Attempt.objects.filter(student=student, test=fresh).first()
        if existing is not None:
            attempt, created = expire_if_overdue(existing, now), False
        else:
            if not fresh.is_open(now):
                raise StateError("This test is not open for new attempts.")
            attempt, created = Attempt.objects.get_or_create(
                student=student,
                test=fresh,
                defaults={"started_at": now, "deadline": now + timedelta(minutes=fresh.duration_minutes)},
            )
    if not attempt.is_in_progress:
        raise AttemptFinalized(attempt)
    return attempt, created


def parse_selections(post):
    """Turn POST data `q_<question_id>=<choice_id>` into {question_id: choice_id}. Anything else is ignored;
    malformed ids or too many selections raise InvalidAnswer."""
    selections = {}
    for key, value in post.items():
        if not key.startswith("q_"):
            continue
        key_match = _ANSWER_KEY.fullmatch(key)
        if key_match is None or _ID_VALUE.fullmatch(value) is None:
            raise InvalidAnswer("Malformed answer.")
        selections[int(key_match.group(1))] = int(value)
    if len(selections) > MAX_SELECTIONS_PER_REQUEST:
        raise InvalidAnswer("Too many answers.")
    return selections


def parse_texts(post):
    """Turn POST data `t_<question_id>=<text>` into {question_id: text} for short-answer questions.
    Over-long or malformed answers raise InvalidAnswer (the form's maxlength stops honest users first)."""
    texts = {}
    for key, value in post.items():
        if not key.startswith("t_"):
            continue
        key_match = _TEXT_KEY.fullmatch(key)
        if key_match is None:
            raise InvalidAnswer("Malformed answer.")
        value = value.replace("\r\n", "\n")
        if len(value) > MAX_SHORT_ANSWER_CHARS or "\x00" in value:
            raise InvalidAnswer("Answer too long or invalid.")
        texts[int(key_match.group(1))] = value.strip()
    if len(texts) > MAX_SELECTIONS_PER_REQUEST:
        raise InvalidAnswer("Too many answers.")
    return texts


def _open_attempt(attempt, now):
    """Re-read the attempt inside a transaction; it must be in progress and before its deadline."""
    current = Attempt.objects.get(pk=attempt.pk)
    if not current.is_in_progress:
        raise StateError("This attempt is already finished.")
    if current.is_past_deadline(now):
        raise StateError("Time is up for this attempt.")
    return current


def record_answers(attempt, selections, texts=None, now=None):
    """Store multiple-choice selections and short-answer texts. All-or-nothing: one bad entry
    rejects the whole request.

    Chain: the attempt is re-read and must be in progress and before its deadline; every question
    must belong to the attempt's test and be of the matching kind; every choice must belong to
    that question. Student, score, marks and timestamps are never inputs here.
    """
    now = now or timezone.now()
    texts = texts or {}
    with transaction.atomic():
        current = _open_attempt(attempt, now)
        for question_id, choice_id in selections.items():
            question = Question.objects.filter(pk=question_id, test_id=current.test_id).first()
            if question is None:
                raise InvalidAnswer("Question does not belong to this test.")
            if not question.is_mcq:
                raise InvalidAnswer("This question does not take a choice.")
            choice = Choice.objects.defer("is_correct").filter(pk=choice_id, question_id=question.pk).first()
            if choice is None:
                raise InvalidAnswer("Choice does not belong to this question.")
            StudentAnswer.objects.update_or_create(
                attempt=current, question=question, defaults={"choice": choice, "text": ""}
            )
        for question_id, text in texts.items():
            question = Question.objects.defer("marking_guide").filter(pk=question_id, test_id=current.test_id).first()
            if question is None:
                raise InvalidAnswer("Question does not belong to this test.")
            if question.kind != Question.Kind.SHORT:
                raise InvalidAnswer("This question does not take typed text.")
            if text:
                StudentAnswer.objects.update_or_create(
                    attempt=current, question=question, defaults={"choice": None, "text": text}
                )
            else:  # clearing the box removes the answer
                StudentAnswer.objects.filter(attempt=current, question=question).delete()


def save_file_answer(attempt, question_id, uploaded, now=None):
    """Attach (or replace) the student's uploaded file for a FILE question.

    The file is validated from its bytes (assessments.uploads) before anything is written; the
    attempt must be in progress and before its deadline, and the question must be a FILE question
    of the attempt's test. Raises uploads.UploadRejected for an unacceptable file.
    """
    now = now or timezone.now()
    filename, content_type, data = uploads.inspect_upload(uploaded)
    with transaction.atomic():
        current = _open_attempt(attempt, now)
        question = Question.objects.defer("marking_guide").filter(pk=question_id, test_id=current.test_id).first()
        if question is None:
            raise InvalidAnswer("Question does not belong to this test.")
        if question.kind != Question.Kind.FILE:
            raise InvalidAnswer("This question does not take a file.")
        answer, _ = StudentAnswer.objects.get_or_create(attempt=current, question=question, defaults={"text": ""})
        AnswerFile.objects.update_or_create(
            answer=answer,
            defaults={"filename": filename, "content_type": content_type, "size": len(data), "content": data},
        )
    return answer


def _score(attempt):
    """(score, max_score) from stored answers: multiple choice by the answer key, short-answer and
    file answers by the marks a lecturer has awarded so far (0 until marked)."""
    max_score = Question.objects.filter(test_id=attempt.test_id).aggregate(t=Sum("marks"))["t"] or 0
    answers = StudentAnswer.objects.filter(attempt=attempt, question__test_id=attempt.test_id)
    auto = (
        answers.filter(question__kind=Question.Kind.MCQ, choice__is_correct=True).aggregate(t=Sum("question__marks"))["t"]
        or 0
    )
    manual = (
        answers.exclude(question__kind=Question.Kind.MCQ).filter(awarded_marks__isnull=False)
        .aggregate(t=Sum("awarded_marks"))["t"]
        or 0
    )
    return auto + manual, max_score


def _finalize(attempt_id, status, now):
    """Score from stored answers and close the attempt exactly once (compare-and-set)."""
    with transaction.atomic():
        attempt = Attempt.objects.get(pk=attempt_id)
        if attempt.is_in_progress:
            score, max_score = _score(attempt)
            Attempt.objects.filter(pk=attempt_id, status=Attempt.Status.IN_PROGRESS).update(
                status=status,
                score=score,
                max_score=max_score,
                submitted_at=now if status == Attempt.Status.SUBMITTED else None,
            )
    return Attempt.objects.get(pk=attempt_id)


def submit_attempt(attempt, now=None):
    """Finish the attempt. At or after the deadline the clock wins: it becomes EXPIRED, scored on
    the answers saved before the deadline. A second call returns the already-final attempt."""
    now = now or timezone.now()
    current = Attempt.objects.get(pk=attempt.pk)
    if not current.is_in_progress:
        return current
    status = Attempt.Status.EXPIRED if current.is_past_deadline(now) else Attempt.Status.SUBMITTED
    return _finalize(current.pk, status, now)


def expire_if_overdue(attempt, now=None):
    now = now or timezone.now()
    if attempt.is_in_progress and attempt.is_past_deadline(now):
        return _finalize(attempt.pk, Attempt.Status.EXPIRED, now)
    return attempt


def expire_overdue_for_student(student, now=None):
    now = now or timezone.now()
    for attempt in Attempt.objects.filter(student=student, status=Attempt.Status.IN_PROGRESS, deadline__lte=now):
        _finalize(attempt.pk, Attempt.Status.EXPIRED, now)


def expire_overdue_for_test(test, now=None):
    """Finalize every overdue in-progress attempt of a test, so a lecturer can mark them."""
    now = now or timezone.now()
    for attempt in Attempt.objects.filter(test=test, status=Attempt.Status.IN_PROGRESS, deadline__lte=now):
        _finalize(attempt.pk, Attempt.Status.EXPIRED, now)


# --- Marking short-answer and file answers ------------------------------------------------

MANUAL_KINDS = (Question.Kind.SHORT, Question.Kind.FILE)


def pending_grading_count(attempt):
    """How many answers on short-answer / file questions still wait for a lecturer's mark."""
    return StudentAnswer.objects.filter(
        attempt=attempt, question__kind__in=MANUAL_KINDS, graded_at__isnull=True
    ).count()


def parse_grades(post, answers):
    """Read `marks_<answer_id>` and `feedback_<answer_id>` for the given answers.

    Returns {answer_id: (marks, feedback)} for answers whose marks box was filled. A blank marks box
    means "not marked yet" and is left alone. Anything non-numeric raises InvalidAnswer.
    """
    grades = {}
    for answer in answers:
        raw = (post.get(f"marks_{answer.pk}") or "").strip()
        if raw == "":
            continue
        if _ID_VALUE.fullmatch(raw) is None:
            raise InvalidAnswer("Marks must be a whole number.")
        feedback = (post.get(f"feedback_{answer.pk}") or "").replace("\r\n", "\n").strip()
        grades[answer.pk] = (int(raw), feedback)
    return grades


def grade_attempt(attempt, grades, grader, now=None):
    """Record a lecturer's marks and feedback, then recompute the attempt's score.

    `grades` is {answer_id: (marks, feedback)}. The attempt must already be finalized (SUBMITTED or
    EXPIRED); every answer must belong to it and be a short-answer or file answer; marks must be
    between 0 and the question's marks. `grader` is the logged-in user, never a form value.
    All-or-nothing. Re-marking is allowed (it overwrites and re-scores).
    """
    now = now or timezone.now()
    with transaction.atomic():
        current = Attempt.objects.select_for_update().get(pk=attempt.pk)
        if current.is_in_progress:
            raise StateError("This attempt is still in progress, so it cannot be marked yet.")
        answers = {
            a.pk: a
            for a in StudentAnswer.objects.select_related("question")
            .defer("question__marking_guide")
            .filter(attempt=current, pk__in=list(grades))
        }
        for answer_id, (marks, feedback) in grades.items():
            answer = answers.get(answer_id)
            if answer is None or answer.question.is_mcq:
                raise InvalidAnswer("That answer cannot be marked here.")
            if not 0 <= marks <= answer.question.marks:
                raise InvalidAnswer(f"Marks must be between 0 and {answer.question.marks}.")
            if len(feedback) > MAX_FEEDBACK_CHARS or "\x00" in feedback:
                raise InvalidAnswer("Feedback is too long or invalid.")
            answer.awarded_marks = marks
            answer.feedback = feedback
            answer.graded_by = grader
            answer.graded_at = now
            answer.save(update_fields=["awarded_marks", "feedback", "graded_by", "graded_at"])
        score, max_score = _score(current)
        Attempt.objects.filter(pk=current.pk).exclude(status=Attempt.Status.IN_PROGRESS).update(
            score=score, max_score=max_score
        )
    return Attempt.objects.get(pk=attempt.pk)
