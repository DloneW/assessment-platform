from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="accounts/login.html", redirect_authenticated_user=True),
        name="login",
    ),
    # Django 5 LogoutView accepts POST only (CSRF-protected).
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("student/dashboard/", views.student_dashboard, name="student_dashboard"),
]
