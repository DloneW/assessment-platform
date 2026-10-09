"""Forms for the in-app administration console. Every input is validated server-side; privilege
fields (is_staff, is_superuser, groups) are never accepted from a request."""

import re
import secrets
import unicodedata

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from authorization.roles import LECTURER, assign_role, users_with_role

from .models import Course, Department, Level

User = get_user_model()

# No look-alike characters (0/O, 1/l/I), so a temporary password can be read out or copied by hand.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_COURSE_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9 /._-]{1,19}")


def generate_temporary_password(user=None):
    """A random 14-character password that also passes the site's password validators."""
    while True:
        candidate = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(14))
        try:
            validate_password(candidate, user)
        except ValidationError:
            continue
        return candidate


def _clean_text(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name"]

    def clean_name(self):
        name = _clean_text(self.cleaned_data["name"])
        if not name:
            raise ValidationError("This field is required.")
        clash = Department.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("A department with this name already exists.")
        return name


class LevelForm(forms.ModelForm):
    class Meta:
        model = Level
        fields = ["number"]
        labels = {"number": "Level (e.g. 100, 200)"}


class CourseForm(forms.ModelForm):
    lecturers = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = Course
        fields = ["code", "title", "department", "level", "is_active", "lecturers"]
        labels = {"is_active": "Open to students"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only active accounts that really hold the LECTURER role can be assigned.
        self.fields["lecturers"].queryset = users_with_role(LECTURER).filter(is_active=True).order_by("last_name", "username")
        self.fields["lecturers"].label_from_instance = lambda u: f"{u.get_full_name() or u.username} ({u.username})"

    def clean_code(self):
        code = _clean_text(self.cleaned_data["code"]).upper()
        if not _COURSE_CODE_RE.fullmatch(code):
            raise ValidationError("Use letters, digits and / - . _ only (2 to 20 characters), e.g. CYB201.")
        if Course.objects.filter(code__iexact=code).exclude(pk=self.instance.pk).exists():
            raise ValidationError("A course with this code already exists.")
        return code

    def clean_title(self):
        title = _clean_text(self.cleaned_data["title"])
        if not title:
            raise ValidationError("This field is required.")
        return title


class LecturerCreateForm(forms.ModelForm):
    """Creates a LECTURER with a random temporary password the admin hands over once."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].required = True
        self.temporary_password = None

    def clean_username(self):
        username = unicodedata.normalize("NFKC", self.cleaned_data["username"])
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    @transaction.atomic
    def save(self):
        user = super().save(commit=False)
        user.is_staff = False
        user.is_superuser = False
        user.must_change_password = True
        self.temporary_password = generate_temporary_password(user)
        user.set_password(self.temporary_password)
        user.save()
        assign_role(user, LECTURER)  # fixed server-side, never read from the request
        return user


class ForcedPasswordChangeForm(PasswordChangeForm):
    """Normal password change, plus: the new password must differ, and the 'temporary' flag is cleared."""

    def clean_new_password1(self):
        new = self.cleaned_data["new_password1"]
        if self.user.check_password(new):
            raise ValidationError("Choose a password different from your current one.")
        return new

    def save(self, commit=True):
        self.user.must_change_password = False
        return super().save(commit=commit)
