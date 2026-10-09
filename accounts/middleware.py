from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import patch_cache_control


class NoStoreForAuthenticatedUsersMiddleware:
    """Mark every response to a logged-in user as non-cacheable.

    Results, the lecturer's answer key and profile data would otherwise stay in the browser
    cache and could be re-opened with the Back button after logout (notably on shared
    computers). Runs after AuthenticationMiddleware, so `request.user` is available.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated:
            patch_cache_control(response, no_store=True, private=True)
            response["Pragma"] = "no-cache"
        return response


class ForcePasswordChangeMiddleware:
    """Hold a user who was given a temporary password on the change-password page.

    Until they choose their own password, every page except that one (and logout) redirects
    there. Runs after AuthenticationMiddleware, so `request.user` is available.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and getattr(user, "must_change_password", False):
            allowed = {reverse("accounts:password_change"), reverse("accounts:logout")}
            if request.path not in allowed:
                return redirect("accounts:password_change")
        return self.get_response(request)
