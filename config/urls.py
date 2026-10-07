from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    # Must come before admin.site.urls: it defines "admin/dashboard/" (application admin
    # dashboard), which Django admin's own "admin/" catch-all would otherwise swallow.
    path("", include("accounts.urls")),
    path("", include("assessments.urls")),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(pattern_name="accounts:login", permanent=False)),
]
