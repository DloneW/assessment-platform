from django.contrib.auth import views as auth_views
from django.urls import path

from . import console, views

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
    path("password/change/", console.PasswordChange.as_view(), name="password_change"),
    # In-app administration console (application ADMIN role only).
    path("manage/departments/", console.departments, name="console_departments"),
    path("manage/departments/<int:pk>/edit/", console.department_edit, name="console_department_edit"),
    path("manage/departments/<int:pk>/delete/", console.department_delete, name="console_department_delete"),
    path("manage/levels/", console.levels, name="console_levels"),
    path("manage/courses/", console.courses, name="console_courses"),
    path("manage/courses/add/", console.course_edit, name="console_course_add"),
    path("manage/courses/<int:pk>/edit/", console.course_edit, name="console_course_edit"),
    path("manage/courses/<int:pk>/enrolment/", console.course_enrolment, name="console_course_enrolment"),
    path("manage/lecturers/", console.lecturers, name="console_lecturers"),
    path("manage/lecturers/add/", console.lecturer_add, name="console_lecturer_add"),
    path("manage/accounts/", console.accounts_list, name="console_accounts"),
    path("manage/accounts/<int:pk>/reset-password/", console.reset_password, name="console_reset_password"),
    path("students/<int:pk>/", views.student_profile_detail, name="student_profile"),
    path("courses/<int:pk>/", views.course_detail, name="course_detail"),
]
