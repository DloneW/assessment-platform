"""Reject oversized request bodies before Django parses them (uploads are small by design)."""

from django.http import HttpResponse

from .models import MAX_UPLOAD_BYTES

# Room for a few small files plus text fields; each file is also checked on its own (assessments.uploads).
MAX_REQUEST_BYTES = 5 * MAX_UPLOAD_BYTES


class MaxUploadSizeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST":
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                return HttpResponse("Bad request.", status=400)
            if length > MAX_REQUEST_BYTES:
                return HttpResponse("Upload too large.", status=413)
        return self.get_response(request)
