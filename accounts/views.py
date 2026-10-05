from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse

from authorization import services
from authorization.enforcement import get_authorized_or_404, require
from authorization.roles import ADMIN, LECTURER, STUDENT, primary_role

from .forms import StudentRegistrationForm
from .models import Course, StudentProfile

User = get_user_model()

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

    def get_default_redirect_url(self):
        return reverse(LANDING_URL_NAMES.get(primary_role(self.request.user), "accounts:dashboard"))


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


@login_required
def dashboard(request):
    """Send the user to the dashboard for their role; users with no role get 403."""
    role = primary_role(request.user)
    if role is None:
        raise PermissionDenied
    return redirect(LANDING_URL_NAMES[role])


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


@require(services.can_access_lecturer_dashboard)
def lecturer_dashboard(request):
    courses = request.user.taught_courses.select_related("department", "level")
    return render(request, "accounts/lecturer_dashboard.html", {"courses": courses})


@require(services.can_access_admin_dashboard)
def admin_dashboard(request):
    # Aggregate counts only; no personal data on this page yet.
    counts = {
        "students": User.objects.filter(groups__name=STUDENT).count(),
        "lecturers": User.objects.filter(groups__name=LECTURER).count(),
        "admins": User.objects.filter(groups__name=ADMIN).count(),
        "courses": Course.objects.count(),
    }
    return render(request, "accounts/admin_dashboard.html", {"counts": counts})


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
