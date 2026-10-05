from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.RoleAwareLoginView.as_view(), name="login"),
    # Django 5 LogoutView accepts POST only (CSRF-protected).
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("student/dashboard/", views.student_dashboard, name="student_dashboard"),
    path("lecturer/dashboard/", views.lecturer_dashboard, name="lecturer_dashboard"),
    # NOTE: this is the application admin dashboard, not the Django admin site. config/urls.py
    # must include these URLs BEFORE admin.site.urls so "admin/dashboard/" is matched first.
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("students/<int:pk>/", views.student_profile_detail, name="student_profile"),
    path("courses/<int:pk>/", views.course_detail, name="course_detail"),
]
