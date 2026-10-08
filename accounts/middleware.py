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
