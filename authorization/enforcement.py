"""
Glue between the decisions in `services.py` and Django views.

Response policy (applied consistently):

  * Anonymous user                          -> redirect to login.
  * Logged in, role-gated page not allowed  -> 403 Forbidden. The page itself is not
    secret (/lecturer/dashboard/ exists for everyone), so being honest costs nothing.
  * Logged in, object not allowed           -> 404 Not Found, identical to a missing
    object. Object IDs are sequential; a 403 would confirm "that profile/course exists"
    and let anyone enumerate them. The 404 body is the same either way.

Error responses never include details about why access was denied.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404


def require(check):
    """View decorator: login required, then `check(user)` must be True, else 403."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not check(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator


def get_authorized_or_404(user, check, queryset, **lookup):
    """Fetch one object and verify `check(user, obj)`; 404 if missing OR not permitted.

    Both failures raise the same bare Http404 so they cannot be told apart.
    """
    try:
        obj = queryset.get(**lookup)
    except queryset.model.DoesNotExist:
        raise Http404 from None
    if not check(user, obj):
        raise Http404
    return obj
