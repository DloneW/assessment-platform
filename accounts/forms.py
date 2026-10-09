import re
import unicodedata

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from authorization.roles import STUDENT, assign_role

from .models import Department, Level, StudentProfile

_MATRIC_RE = re.compile(r"[A-Z0-9][A-Z0-9/._-]{2,29}")

User = get_user_model()


class StudentRegistrationForm(forms.ModelForm):
    """
    Registers a User plus its StudentProfile atomically.

    Privilege fields (is_staff, is_superuser, ...) are NOT in this form, and
    save() never sets them, so registrants are always plain students.
    """

    # The student's own matric number. Formats differ between students and departments, so no
    # single pattern is enforced; only a safe character set, case-insensitive uniqueness and
    # normalisation (stored upper-case, no spaces) are.
    student_id = forms.CharField(
        max_length=30,
        label="Matric number",
        help_text="Your own matric number, exactly as on your school ID (letters, digits, / - . _).",
    )
    department = forms.ModelChoiceField(queryset=Department.objects.all(), empty_label="Select department")
    level = forms.ModelChoiceField(queryset=Level.objects.all(), empty_label="Select level")
    password1 = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", strip=False, widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("first_name", "last_name", "email"):
            self.fields[name].required = True

    def clean_username(self):
        # NFKC like Django's login form, so what is stored is what a user can later type;
        # case-insensitive uniqueness stops "Alice" registering beside "alice".
        username = unicodedata.normalize("NFKC", self.cleaned_data["username"])
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_student_id(self):
        student_id = unicodedata.normalize("NFKC", self.cleaned_data["student_id"]).strip().upper()
        if not student_id:
            raise ValidationError("This field is required.")
        if not _MATRIC_RE.fullmatch(student_id):
            raise ValidationError(
                "Enter your matric number using only letters, digits and / - . _ (3 to 30 characters, no spaces)."
            )
        if StudentProfile.objects.filter(student_id__iexact=student_id).exists():
            raise ValidationError("A student with this matric number already exists.")
        return student_id

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two password fields didn't match.")
        elif password1:
            # Build an unsaved user so similarity validation can use the form's own data.
            candidate = User(
                username=cleaned.get("username", ""),
                email=cleaned.get("email", ""),
                first_name=cleaned.get("first_name", ""),
                last_name=cleaned.get("last_name", ""),
            )
            try:
                validate_password(password1, candidate)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    @transaction.atomic
    def save(self):
        """Create the User and its StudentProfile together; always commits (no commit=False)."""
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])  # Django hashing; never plaintext
        user.is_staff = False
        user.is_superuser = False
        user.save()
        assign_role(user, STUDENT)  # fixed server-side; never read from the request
        StudentProfile.objects.create(
            user=user,
            student_id=self.cleaned_data["student_id"],
            department=self.cleaned_data["department"],
            level=self.cleaned_data["level"],
        )
        return user
