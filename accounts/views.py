from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from authorization import services
from authorization.enforcement import get_authorized_or_404, require
from authorization.roles import ADMIN, LECTURER, STUDENT, primary_role, users_with_role

from .forms import StudentRegistrationForm
from .models import Course, StudentProfile

# HTTP method policy: nothing at the read-only URLs below modifies data, so they accept GET/HEAD
# only (@require_safe), and the method gate is the OUTERMOST decorator. That way a probing verb
# gets the same 405 for every user and every object ID; it cannot be used to tell an existing
# object from a missing one, and it never reaches the authorization checks or the database.

# Where each role lands after login. A user holding several roles lands on the highest.
LANDING_URL_NAMES = {
    ADMIN: "accounts:admin_dashboard",
    LECTURER: "accounts:lecturer_dashboard",
    STUDENT: "accounts:student_dashboard",
}


class RoleAwareLoginView(LoginView):
    """Django's LoginView, but the default destination depends on the user's role."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True
    http_method_names = ["get", "head", "post", "options"]  # Django's FormView would also accept PUT

    def get_default_redirect_url(self):
        return reverse(LANDING_URL_NAMES.get(primary_role(self.request.user), "accounts:dashboard"))


@require_http_methods(["GET", "HEAD", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    if request.method == "POST":
        form = StudentRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Registration successful. Please log in.")
            return redirect("accounts:login")
    else:
        form = StudentRegistrationForm()
    return render(request, "accounts/register.html", {"form": form})


@require_safe
@require(services.can_access_dashboard)
def dashboard(request):
    """Send the user to the dashboard for their role (accounts with no role get 403)."""
    return redirect(LANDING_URL_NAMES[primary_role(request.user)])


@require_safe
@require(services.can_access_student_dashboard)
def student_dashboard(request):
    # The session determines the student; nothing identifying comes from the URL.
    try:
        profile = request.user.student_profile
    except StudentProfile.DoesNotExist:
        profile = None
    courses = []
    if profile is not None:
        courses = [
            course
            for course in profile.courses.select_related("department", "level")
            if services.can_access_course(request.user, course)
        ]
    return render(request, "accounts/student_dashboard.html", {"profile": profile, "courses": courses})


@require_safe
@require(services.can_access_lecturer_dashboard)
def lecturer_dashboard(request):
    courses = request.user.taught_courses.select_related("department", "level")
    return render(request, "accounts/lecturer_dashboard.html", {"courses": courses})


@require_safe
@require(services.can_access_admin_dashboard)
def admin_dashboard(request):
    # Aggregate counts only; no personal data on this page yet.
    counts = {
        "students": users_with_role(STUDENT).count(),
        "lecturers": users_with_role(LECTURER).count(),
        "admins": users_with_role(ADMIN).count(),
        "courses": Course.objects.count(),
    }
    return render(request, "accounts/admin_dashboard.html", {"counts": counts})


@require_safe
@login_required
def student_profile_detail(request, pk):
    profile = get_authorized_or_404(
        request.user,
        services.can_view_student_profile,
        StudentProfile.objects.select_related("user", "department", "level"),
        pk=pk,
    )
    courses = profile.courses.select_related("department", "level")
    return render(request, "accounts/student_profile.html", {"profile": profile, "courses": courses})


@require_safe
@login_required
def course_detail(request, pk):
    course = get_authorized_or_404(
        request.user,
        services.can_access_course,
        Course.objects.select_related("department", "level"),
        pk=pk,
    )
    context = {
        "course": course,
        "can_manage": services.can_manage_course(request.user, course),
    }
    if context["can_manage"]:
        context["enrolled_count"] = course.enrollments.count()
    return render(request, "accounts/course_detail.html", context)
