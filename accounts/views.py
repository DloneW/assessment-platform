from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import StudentRegistrationForm
from .models import StudentProfile


def register(request):
    if request.user.is_authenticated:
        return redirect("accounts:student_dashboard")

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
def student_dashboard(request):
    # The session determines the student; nothing identifying comes from the URL.
    try:
        profile = request.user.student_profile
    except StudentProfile.DoesNotExist:
        profile = None
    return render(request, "accounts/student_dashboard.html", {"profile": profile})
