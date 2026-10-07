"""
The authorization decisions views call. This is the ONLY place that decides who
may do what.

Each decision reads top-to-bottom as the pipeline:

    authenticated? -> role? -> attributes? -> ownership/management? -> resource state?

All functions return a plain bool, are side-effect free, and take the user from
`request.user` (the server-side session), never from request data.

Day 3 will add `can_access_test`, `can_view_attempt`, `can_edit_test` and
`can_publish_test` here by composing these same rules with the Test/Attempt
models (course access + ownership + publication state/availability window).
"""

from . import rules
from .roles import ADMIN, LECTURER, STUDENT, has_role, primary_role


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
