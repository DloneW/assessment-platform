"""
Assessment data model.

Lifecycle:  Test (DRAFT) -> PUBLISHED -> ARCHIVED.  A published test is frozen (no question
or choice edits), so scoring can never change underneath existing attempts.

Integrity is enforced in the database wherever SQL can express it (unique and check
constraints below). Two rules span several tables, which plain constraints cannot express,
so `StudentAnswer.save()` enforces them: the question must belong to the attempt's test, and
the choice must belong to that question. All writes go through `assessments.services`.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

MAX_DURATION_MINUTES = 600
MAX_SHORT_ANSWER_CHARS = 2000
MAX_FEEDBACK_CHARS = 1000
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB; files are stored in the database


class Test(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        ARCHIVED = "ARCHIVED", "Archived"

    # PROTECT: a course with tests cannot be deleted by accident.
    course = models.ForeignKey("accounts.Course", on_delete=models.PROTECT, related_name="tests")
    # Set by the server from the logged-in lecturer; never read from a form.
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_tests")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, help_text="Instructions shown to students before they start.")
    duration_minutes = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(MAX_DURATION_MINUTES)]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # Optional window during which students may START the test (an attempt's own deadline is
    # started_at + duration, regardless of this window).
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(duration_minutes__gte=1, duration_minutes__lte=MAX_DURATION_MINUTES),
                name="assessments_test_duration_valid",
            ),
            models.CheckConstraint(condition=~Q(title=""), name="assessments_test_title_not_blank"),
            models.CheckConstraint(
                condition=Q(opens_at__isnull=True) | Q(closes_at__isnull=True) | Q(closes_at__gt=F("opens_at")),
                name="assessments_test_window_valid",
            ),
            # A draft has never been published; a published or archived test always has a publication time.
            models.CheckConstraint(
                condition=Q(status="DRAFT", published_at__isnull=True)
                | Q(status__in=["PUBLISHED", "ARCHIVED"], published_at__isnull=False),
                name="assessments_test_publication_state_valid",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.course.code})"

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    def is_open(self, now=None):
        """Published, and `now` falls inside the optional start window."""
        now = now or timezone.now()
        if not self.is_published:
            return False
        if self.opens_at is not None and now < self.opens_at:
            return False
        if self.closes_at is not None and now >= self.closes_at:
            return False
        return True


class Question(models.Model):
    class Kind(models.TextChoices):
        MCQ = "MCQ", "Multiple choice"  # auto-scored from the answer key
        SHORT = "SHORT", "Short answer"  # typed text, marked by a lecturer
        FILE = "FILE", "File upload"  # an image or document, marked by a lecturer

    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name="questions")
    kind = models.CharField(max_length=5, choices=Kind.choices, default=Kind.MCQ)
    text = models.TextField()
    order = models.PositiveSmallIntegerField()
    marks = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    # Lecturer-only notes for marking SHORT/FILE questions. Server-side data like the answer key:
    # student-facing code must never load or render it (it is deferred in every student query).
    marking_guide = models.TextField(blank=True)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["test", "order"], name="assessments_question_order_unique"),
            models.CheckConstraint(condition=Q(marks__gte=1), name="assessments_question_marks_positive"),
            models.CheckConstraint(condition=~Q(text=""), name="assessments_question_text_not_blank"),
            models.CheckConstraint(condition=Q(order__gte=1), name="assessments_question_order_positive"),
            models.CheckConstraint(condition=Q(kind__in=["MCQ", "SHORT", "FILE"]), name="assessments_question_kind_valid"),
        ]

    def __str__(self):
        return f"Q{self.order}: {self.text[:40]}"

    @property
    def is_mcq(self):
        return self.kind == self.Kind.MCQ

    @property
    def needs_manual_grading(self):
        return self.kind != self.Kind.MCQ


class Choice(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="choices")
    text = models.CharField(max_length=500)
    order = models.PositiveSmallIntegerField()
    # Answer key. Server-side data: student-facing code must never load or render it.
    is_correct = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["question", "order"], name="assessments_choice_order_unique"),
            models.CheckConstraint(condition=~Q(text=""), name="assessments_choice_text_not_blank"),
            models.CheckConstraint(condition=Q(order__gte=1), name="assessments_choice_order_positive"),
            # At most one correct choice per question. "Exactly one" (so at least one) is checked when publishing.
            models.UniqueConstraint(
                fields=["question"], condition=Q(is_correct=True), name="assessments_choice_one_correct_per_question"
            ),
        ]

    def __str__(self):
        return self.text[:40]


class Attempt(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        SUBMITTED = "SUBMITTED", "Submitted"
        EXPIRED = "EXPIRED", "Expired"

    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.PROTECT, related_name="attempts")
    test = models.ForeignKey(Test, on_delete=models.PROTECT, related_name="attempts")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS)
    # All timestamps and the score are set by the server and cannot be edited through any form.
    started_at = models.DateTimeField(default=timezone.now, editable=False)
    # Fixed when the attempt starts (started_at + duration), so later edits cannot move it.
    deadline = models.DateTimeField(editable=False)
    # Set only for SUBMITTED attempts. EXPIRED attempts were ended by the clock, not by the student.
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Result. Both are NULL while in progress and set together when the attempt is finalized.
    score = models.PositiveIntegerField(null=True, blank=True, editable=False)
    max_score = models.PositiveIntegerField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            # MVP policy: one attempt per student per test (so never two active ones, and no retakes).
            models.UniqueConstraint(fields=["student", "test"], name="assessments_attempt_one_per_student_test"),
            models.CheckConstraint(condition=Q(deadline__gt=F("started_at")), name="assessments_attempt_deadline_after_start"),
            # Status and result move together.
            models.CheckConstraint(
                condition=Q(status="IN_PROGRESS", score__isnull=True, max_score__isnull=True, submitted_at__isnull=True)
                | Q(status="SUBMITTED", score__isnull=False, max_score__isnull=False, submitted_at__isnull=False)
                | Q(status="EXPIRED", score__isnull=False, max_score__isnull=False, submitted_at__isnull=True),
                name="assessments_attempt_state_valid",
            ),
            models.CheckConstraint(
                condition=Q(score__isnull=True) | Q(score__lte=F("max_score")), name="assessments_attempt_score_within_max"
            ),
            # Time must make sense: never submitted before it started, and a SUBMITTED attempt was
            # handed in strictly before the deadline (at or after it the clock expires the attempt).
            models.CheckConstraint(
                condition=Q(submitted_at__isnull=True) | Q(submitted_at__gte=F("started_at")),
                name="assessments_attempt_submitted_after_start",
            ),
            models.CheckConstraint(
                condition=~Q(status="SUBMITTED") | Q(submitted_at__lt=F("deadline")),
                name="assessments_attempt_submitted_before_deadline",
            ),
        ]

    def __str__(self):
        return f"{self.student.student_id} on {self.test_id} ({self.status})"

    @property
    def is_in_progress(self):
        return self.status == self.Status.IN_PROGRESS

    def is_past_deadline(self, now=None):
        return (now or timezone.now()) >= self.deadline

    @property
    def percentage(self):
        if self.score is None or not self.max_score:
            return None
        return round(100 * self.score / self.max_score, 1)


class StudentAnswer(models.Model):
    """A student's answer to one question in one attempt (at most one per question).

    MCQ: `choice` is set. SHORT: `text` is set. FILE: an `AnswerFile` row is attached.
    SHORT and FILE answers are marked by a lecturer: `awarded_marks`, `feedback`, `graded_by` and
    `graded_at` are set together by the grading workflow and never by the student.
    """

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(Question, on_delete=models.PROTECT, related_name="answers")
    choice = models.ForeignKey(Choice, on_delete=models.PROTECT, related_name="answers", null=True, blank=True)
    text = models.TextField(blank=True)
    answered_at = models.DateTimeField(auto_now=True)
    awarded_marks = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    feedback = models.TextField(blank=True, editable=False)
    graded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False
    )
    graded_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["attempt", "question"], name="assessments_answer_one_per_attempt_question"),
            # A mark and its grading time exist together.
            models.CheckConstraint(
                condition=Q(awarded_marks__isnull=True, graded_at__isnull=True)
                | Q(awarded_marks__isnull=False, graded_at__isnull=False),
                name="assessments_answer_grade_consistent",
            ),
            models.CheckConstraint(
                condition=Q(text="") | Q(choice__isnull=True), name="assessments_answer_choice_xor_text"
            ),
        ]

    def __str__(self):
        return f"attempt {self.attempt_id}, question {self.question_id}"

    GRADING_FIELDS = frozenset({"awarded_marks", "feedback", "graded_by", "graded_at"})

    def check_integrity(self, *, grading=False):
        """Rules that span tables and so cannot be plain SQL constraints.

        `grading=True` is a save that touches only the grading fields (after the attempt has been
        finalized): the answer's content is still frozen, but marking it must be possible.
        """
        if self.question.test_id != self.attempt.test_id:
            raise ValidationError("The question does not belong to the attempt's test.")
        if self.question.is_mcq:
            if self.choice_id is None or self.choice.question_id != self.question_id:
                raise ValidationError("The choice does not belong to the question.")
            if self.text:
                raise ValidationError("A multiple-choice answer has no text.")
        else:
            if self.choice_id is not None:
                raise ValidationError("Only multiple-choice questions take a choice.")
            if len(self.text) > MAX_SHORT_ANSWER_CHARS:
                raise ValidationError("The answer is too long.")
        if self.awarded_marks is not None and self.awarded_marks > self.question.marks:
            raise ValidationError("Awarded marks cannot exceed the question's marks.")
        # Ask the database, not the in-memory object: a stale instance must not keep a closed attempt writable.
        # (Grading happens after the attempt is finalized, so it is exempt: only the answer content is frozen.)
        if not grading and not Attempt.objects.filter(pk=self.attempt_id, status=Attempt.Status.IN_PROGRESS).exists():
            raise ValidationError("The attempt is no longer in progress.")

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        grading = update_fields is not None and set(update_fields) <= self.GRADING_FIELDS
        self.check_integrity(grading=grading)
        super().save(*args, **kwargs)


class AnswerFile(models.Model):
    """The file a student uploaded for a FILE question, stored in the database (small files only).

    One per answer. `content_type` and the extension come from the server's own sniffing of the
    bytes (see assessments.uploads), never from what the browser claimed.
    """

    answer = models.OneToOneField(StudentAnswer, on_delete=models.CASCADE, related_name="file")
    filename = models.CharField(max_length=100)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    content = models.BinaryField()
    uploaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(size__gte=1, size__lte=MAX_UPLOAD_BYTES), name="assessments_answerfile_size_valid"
            ),
        ]

    def __str__(self):
        return f"{self.filename} ({self.size} bytes)"
