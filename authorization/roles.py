"""
Application roles.

A role is membership of a Django Group with one of the names below. This is
deliberately separate from Django's `is_staff` / `is_superuser`, which only
control access to the Django admin site:

  * an application ADMIN is NOT automatically staff or a superuser;
  * a superuser is NOT automatically an application ADMIN.

Roles are checked by explicit group membership (not `user.has_perm()`, which
returns True for every permission when the user is a superuser).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction

STUDENT = "STUDENT"
LECTURER = "LECTURER"
ADMIN = "ADMIN"

ALL_ROLES = (STUDENT, LECTURER, ADMIN)

# Used only to decide where a user lands after login if they hold several roles.
LANDING_PRECEDENCE = (ADMIN, LECTURER, STUDENT)


def holds_role(user, role):
    """True if `user` is a member of the role's group, whether or not the account is active.

    Use this to PROTECT accounts (e.g. in the Django admin); use `has_role` to AUTHORIZE requests.
    """
    return user.groups.filter(name=role).exists()


def has_role(user, role):
    """True if `user` is an authenticated, active member of the role's group.

    Queries the database every time (no caching), so removing a role takes
    effect on the very next request.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    return holds_role(user, role)


def users_with_role(role):
    """Queryset of users holding `role`. The one place that maps a role to its group name for queries."""
    return get_user_model().objects.filter(groups__name=role)


def primary_role(user):
    """The highest-precedence role the user holds, or None."""
    for role in LANDING_PRECEDENCE:
        if has_role(user, role):
            return role
    return None


@transaction.atomic
def assign_role(user, role):
    """Give `user` exactly one application role, replacing any other.

    This is a TRUSTED operation: call it only from server-side code paths
    (registration -> STUDENT, the `assign_role` management command, tests).
    Never call it with a role taken from request data.
    """
    if role not in ALL_ROLES:
        raise ValueError(f"Unknown role {role!r}; expected one of {ALL_ROLES}.")
    group, _ = Group.objects.get_or_create(name=role)
    others = Group.objects.filter(name__in=ALL_ROLES).exclude(pk=group.pk)
    user.groups.remove(*others)
    user.groups.add(group)
