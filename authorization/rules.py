"""
Small, single-purpose rules. Each answers ONE question about users, attributes
or resources. They never look at request data and never decide access on their
own; `services.py` combines them into the decisions views call.

Everything here compares database identifiers/relationships (department_id,
level_id, enrollments, assigned lecturers). Nothing is hardcoded to a specific
department name, course code, or student ID.
"""


def student_profile_of(user):
    """The user's own StudentProfile, or None (anonymous users and users without one)."""
    # RelatedObjectDoesNotExist subclasses AttributeError, so the default is honoured.
    return getattr(user, "student_profile", None)


# --- Attribute rules (student <-> course) ---------------------------------------

def department_matches(profile, course):
    return profile.department_id == course.department_id


def level_matches(profile, course):
    # Exact match on purpose. To allow e.g. carry-over of lower-level courses later,
    # change it here and every decision follows.
    return profile.level_id == course.level_id


def is_enrolled(profile, course):
    return course.enrollments.filter(student=profile).exists()


# --- Resource-state rule --------------------------------------------------------

def course_is_open(course):
    return course.is_active


# --- Ownership / management rule ------------------------------------------------

def is_assigned_lecturer(user, course):
    return course.lecturers.filter(pk=user.pk).exists()


# --- Object ownership rule ------------------------------------------------------

def owns_student_profile(user, profile):
    return profile.user_id == user.pk
