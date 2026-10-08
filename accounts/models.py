from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower

from authorization.roles import LECTURER


class User(AbstractUser):
    """
    Custom user model (set from day one so it never needs swapping later).

    Extends AbstractUser: username, email, first_name, last_name, password
    (hashed by Django), is_active, is_staff, is_superuser, date_joined.

    Email uniqueness is case-insensitive and enforced by the database via the
    Lower("email") constraint below (which also implies exact-match uniqueness).
    Roles (lecturer/admin) are deliberately NOT modelled yet; that is Day 2.
    """

    email = models.EmailField("email address")

    objects = UserManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message="A user with this email already exists.",
            ),
            models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_username_ci_unique",
                violation_error_message="A user with this username already exists.",
            ),
        ]

    def __str__(self):
        return self.username


class Department(models.Model):
    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Level(models.Model):
    """Academic level, e.g. 100, 200, 300, 400."""

    number = models.PositiveSmallIntegerField(unique=True, validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"{self.number} Level"


class Course(models.Model):
    code = models.CharField(max_length=20, unique=True)
    title = models.CharField(max_length=200)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="courses")
    level = models.ForeignKey(Level, on_delete=models.PROTECT, related_name="courses")
    # Resource state: students cannot reach an inactive course.
    is_active = models.BooleanField(default=True)
    # Ownership/management: the lecturers who teach this course. Assignment is made by
    # administrators; the authorization layer also re-checks the LECTURER role, so being
    # listed here is never enough on its own.
    lecturers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="taught_courses",
        limit_choices_to={"groups__name": LECTURER},
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.title}"


class StudentProfile(models.Model):
    """
    Academic identity of a student. Department and level are foreign keys
    (not copied strings) so Day 2's attribute-based rules can compare
    profile.department / profile.level against course/test attributes.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_profile"
    )
    student_id = models.CharField(max_length=30)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="students")
    level = models.ForeignKey(Level, on_delete=models.PROTECT, related_name="students")
    # Courses the student is enrolled in, via the explicit Enrollment model below.
    courses = models.ManyToManyField(
        Course, through="Enrollment", blank=True, related_name="students"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("student_id"),
                name="accounts_studentprofile_student_id_ci_unique",
                violation_error_message="A student with this ID already exists.",
            ),
        ]

    def __str__(self):
        return f"{self.student_id} ({self.user.username})"


class Enrollment(models.Model):
    """A student's enrollment in a course.

    Kept deliberately small. Later it can grow fields such as status, academic
    session, or semester without changing how the rest of the code uses it.
    """

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrollments")
    # PROTECT: a course with enrollment records cannot be deleted by accident.
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["course__code"]
        constraints = [
            models.UniqueConstraint(fields=["student", "course"], name="accounts_enrollment_student_course_unique"),
        ]

    def __str__(self):
        return f"{self.student.student_id} in {self.course.code}"
