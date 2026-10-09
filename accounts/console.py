"""In-app administration console (application ADMIN role only).

Replaces day-to-day use of the Django admin site, so the superuser account does not have to be
used for routine work. Every view is gated by `services.can_manage_directory`, mutations are
POST-only (CSRF-protected), and password resets follow `services.can_reset_password`.
Objects an admin may not act on return the same 404 as a missing one.
"""

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import PasswordChangeView
from django.db.models import Count, ProtectedError, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from authorization import services
from authorization.enforcement import get_authorized_or_404, require
from authorization.roles import LECTURER, STUDENT, users_with_role

from .console_forms import (
    CourseForm,
    DepartmentForm,
    ForcedPasswordChangeForm,
    LecturerCreateForm,
    LevelForm,
    generate_temporary_password,
)
from .models import Course, Department, Level

logger = logging.getLogger("accounts.console")
User = get_user_model()

console_only = require(services.can_manage_directory)


# --- Departments --------------------------------------------------------------------

@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def departments(request):
    form = DepartmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        dept = form.save()
        logger.info("console: user %s created department %s", request.user.pk, dept.pk)
        messages.success(request, f"Department “{dept.name}” added.")
        return redirect("accounts:console_departments")
    rows = Department.objects.annotate(n_courses=Count("courses", distinct=True), n_students=Count("students", distinct=True))
    return render(request, "console/departments.html", {"form": form, "departments": rows})


@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def department_edit(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    form = DepartmentForm(request.POST or None, instance=dept)
    if request.method == "POST" and form.is_valid():
        form.save()
        logger.info("console: user %s renamed department %s", request.user.pk, dept.pk)
        messages.success(request, "Department renamed.")
        return redirect("accounts:console_departments")
    return render(request, "console/form.html", {"form": form, "title": f"Rename department: {dept.name}", "back": "accounts:console_departments"})


@require_POST
@console_only
def department_delete(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    try:
        dept.delete()
    except ProtectedError:
        messages.error(request, f"“{dept.name}” still has courses or students, so it cannot be deleted.")
    else:
        logger.info("console: user %s deleted department %s", request.user.pk, pk)
        messages.success(request, "Department deleted.")
    return redirect("accounts:console_departments")


# --- Levels -------------------------------------------------------------------------

@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def levels(request):
    form = LevelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        level = form.save()
        logger.info("console: user %s created level %s", request.user.pk, level.pk)
        messages.success(request, f"{level} added.")
        return redirect("accounts:console_levels")
    return render(request, "console/levels.html", {"form": form, "levels": Level.objects.all()})


# --- Courses ------------------------------------------------------------------------

@require_safe
@console_only
def courses(request):
    rows = Course.objects.select_related("department", "level").prefetch_related("lecturers")
    return render(request, "console/courses.html", {"courses": rows})


@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def course_edit(request, pk=None):
    course = get_object_or_404(Course, pk=pk) if pk else None
    form = CourseForm(request.POST or None, instance=course)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        logger.info("console: user %s %s course %s", request.user.pk, "updated" if course else "created", saved.pk)
        messages.success(request, f"Course {saved.code} saved.")
        return redirect("accounts:console_courses")
    title = f"Edit course {course.code}" if course else "Add course"
    return render(request, "console/form.html", {"form": form, "title": title, "back": "accounts:console_courses"})


# --- Lecturers ----------------------------------------------------------------------

@require_safe
@console_only
def lecturers(request):
    rows = users_with_role(LECTURER).prefetch_related("taught_courses").order_by("last_name", "username")
    return render(request, "console/lecturers.html", {"lecturers": rows})


@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def lecturer_add(request):
    form = LecturerCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        logger.info("console: user %s created lecturer %s", request.user.pk, user.pk)
        # Rendered directly (not redirected, not stored in the session or messages), so the
        # temporary password is shown exactly once.
        return render(request, "console/temp_password.html", {
            "account": user, "temporary_password": form.temporary_password, "created": True,
        })
    return render(request, "console/form.html", {
        "form": form, "title": "Add lecturer", "back": "accounts:console_lecturers",
        "note": "A one-time temporary password is generated; the lecturer must replace it at first login.",
    })


# --- Accounts and password resets ------------------------------------------------------

@require_safe
@console_only
def accounts_list(request):
    q = request.GET.get("q", "").strip()[:100]
    rows = User.objects.filter(
        Q(groups__name=STUDENT) | Q(groups__name=LECTURER), is_superuser=False, is_staff=False
    ).distinct().order_by("username")
    if q:
        rows = rows.filter(
            Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q)
            | Q(last_name__icontains=q) | Q(student_profile__student_id__icontains=q)
        )
    rows = [u for u in rows[:200] if services.can_reset_password(request.user, u)]
    return render(request, "console/accounts.html", {"accounts": rows, "q": q})


@require_http_methods(["GET", "HEAD", "POST"])
@console_only
def reset_password(request, pk):
    target = get_authorized_or_404(request.user, services.can_reset_password, User.objects.all(), pk=pk)
    if request.method == "POST":
        temporary = generate_temporary_password(target)
        target.set_password(temporary)
        target.must_change_password = True
        target.save(update_fields=["password", "must_change_password"])
        # Changing the password invalidates the target's existing sessions (session auth hash).
        logger.info("console: user %s reset password of user %s", request.user.pk, target.pk)
        return render(request, "console/temp_password.html", {
            "account": target, "temporary_password": temporary, "created": False,
        })
    return render(request, "console/reset_confirm.html", {"account": target})


# --- Own password (also the landing page for temporary passwords) -----------------------

class PasswordChange(PasswordChangeView):
    form_class = ForcedPasswordChangeForm
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:dashboard")
    http_method_names = ["get", "head", "post", "options"]

    def form_valid(self, form):
        messages.success(self.request, "Your password has been changed.")
        return super().form_valid(form)
