"""
The authorization decisions views call. This is the ONLY place that decides who
may do what.

Each decision reads top-to-bottom as the pipeline:

    authenticated? -> role? -> attributes? -> ownership/management? -> resource state?

All functions return a plain bool, are side-effect free, and take the user from
`request.user` (the server-side session), never from request data.

Day 3 adds the assessment decisions at the bottom (tests and attempts) by composing the
same rules with the Test/Attempt models: course access + ownership + publication state +
availability window + attempt state.
"""

from django.utils import timezone

from . import rules
from .roles import ADMIN, LECTURER, STUDENT, has_role, holds_role, primary_role


# --- Role-gated pages (no object involved) --------------------------------------

def can_access_dashboard(user):
    """The /dashboard/ router: any account that holds at least one application role."""
    return primary_role(user) is not None


def can_access_student_dashboard(user):
    return has_role(user, STUDENT)


def can_access_lecturer_dashboard(user):
    return has_role(user, LECTURER)


def can_access_admin_dashboard(user):
    return has_role(user, ADMIN)


# --- Administration console (departments, courses, lecturers, password resets) -----

def can_manage_directory(user):
    """The in-app console: only an application ADMIN. Not implied by is_staff / is_superuser."""
    return has_role(user, ADMIN)


def can_reset_password(actor, target):
    """An ADMIN may issue a temporary password only to an ordinary student or lecturer account.

    Never to themselves (they use the normal password-change page), and never to a superuser,
    a staff member or another ADMIN: resetting a privileged account's password is a takeover of
    that account, so those stay a deliberate, out-of-band operation.
    """
    if not has_role(actor, ADMIN) or target.pk == actor.pk:
        return False
    if target.is_superuser or target.is_staff or holds_role(target, ADMIN):
        return False
    return holds_role(target, STUDENT) or holds_role(target, LECTURER)


# --- Object-level: student profiles ---------------------------------------------

def can_view_student_profile(user, profile):
    """A student sees only their own profile; application admins may see any."""
    if has_role(user, ADMIN):
        return True
    return has_role(user, STUDENT) and rules.owns_student_profile(user, profile)


# --- Object-level: courses -------------------------------------------------------

def can_manage_course(user, course):
    """Admins manage every course; a lecturer manages only courses they are assigned to."""
    if has_role(user, ADMIN):
        return True
    return has_role(user, LECTURER) and rules.is_assigned_lecturer(user, course)


def can_take_course(user, course):
    """Eligibility, before enrollment is considered.

    An active student whose department AND level match an open course.
    """
    if not has_role(user, STUDENT):
        return False
    profile = rules.student_profile_of(user)
    if profile is None:
        return False
    return (
        rules.course_is_open(course)
        and rules.department_matches(profile, course)
        and rules.level_matches(profile, course)
    )


def _student_can_access_course(user, course):
    """Eligible AND enrolled. Being enrolled alone is not enough if attributes no longer match."""
    return can_take_course(user, course) and rules.is_enrolled(
        rules.student_profile_of(user), course
    )


def can_access_course(user, course):
    """May `user` open this course?  Managers (admin / assigned lecturer) or eligible, enrolled students."""
    return can_manage_course(user, course) or _student_can_access_course(user, course)


# --- Assessments (Day 3) -----------------------------------------------------------
#
# Lecturers manage tests of courses they are assigned to (admins: any course). Students
# reach a test only through the same course rules as Day 2 AND only while it is published.
# `can_*` answer "is this action allowed right now?"; content rules (e.g. "every question has
# a correct choice") are validated by assessments.services, not here.

def can_create_test(user, course):
    """Manage the course, and the course must be open (no new tests on an inactive course)."""
    return can_manage_course(user, course) and rules.course_is_open(course)


def can_manage_test(user, test):
    return can_manage_course(user, test.course)


def can_edit_test(user, test):
    """Only drafts are editable; publishing freezes questions so scoring cannot shift under attempts."""
    return can_manage_test(user, test) and rules.assessment_is_draft(test)


def can_publish_test(user, test):
    return can_edit_test(user, test) and rules.course_is_open(test.course)


def can_archive_test(user, test):
    return can_manage_test(user, test) and rules.assessment_is_published(test)


def can_take_test(user, test):
    """Student side of access: a published test of a course the student may access (window not considered)."""
    return (
        has_role(user, STUDENT)
        and rules.assessment_is_published(test)
        and _student_can_access_course(user, test.course)
    )


def can_access_test(user, test):
    """Managers see any state; students see only published tests of courses they may access."""
    return can_manage_test(user, test) or can_take_test(user, test)


def can_start_attempt(user, test, now=None):
    """A student who can take the test, while it is open. (One-attempt rule: enforced by the DB.)"""
    return can_take_test(user, test) and rules.assessment_is_open(test, now)


def can_view_attempt(user, attempt):
    """Only the student who owns the attempt (lecturer result views are not part of Day 3)."""
    return has_role(user, STUDENT) and rules.owns_attempt(user, attempt)


def can_answer_attempt(user, attempt, now=None):
    now = now or timezone.now()
    return (
        can_view_attempt(user, attempt)
        and rules.attempt_is_in_progress(attempt)
        and rules.attempt_before_deadline(attempt, now)
    )


def can_submit_attempt(user, attempt):
    """Owner of an attempt that is still in progress. A late submit is accepted here and then
    finalized as EXPIRED by the service, so the clock decides, not this check."""
    return can_view_attempt(user, attempt) and rules.attempt_is_in_progress(attempt)


def can_grade_attempt(user, attempt):
    """A manager of the attempt's course may mark it, but only once it is finished (SUBMITTED or
    EXPIRED), so marks never race with a student still answering."""
    return can_manage_course(user, attempt.test.course) and not rules.attempt_is_in_progress(attempt)


def can_download_answer_file(user, attempt):
    """The student who uploaded it, or a manager of the course the test belongs to."""
    return can_view_attempt(user, attempt) or can_manage_course(user, attempt.test.course)


# --- Querysets that mirror the decisions above (for listings) ---------------------------

def courses_managed_by(user):
    """Courses `user` may manage; mirrors can_manage_course."""
    from accounts.models import Course

    if has_role(user, ADMIN):
        return Course.objects.all()
    if has_role(user, LECTURER):
        return Course.objects.filter(lecturers=user)
    return Course.objects.none()


def courses_accessible_to_student(user):
    """Courses a student may access; mirrors _student_can_access_course."""
    from accounts.models import Course

    profile = rules.student_profile_of(user)
    if profile is None or not has_role(user, STUDENT):
        return Course.objects.none()
    return Course.objects.filter(
        is_active=True,
        department_id=profile.department_id,
        level_id=profile.level_id,
        enrollments__student=profile,
    )
