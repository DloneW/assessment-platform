from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Department, Level, StudentProfile

User = get_user_model()


class StudentRegistrationForm(forms.ModelForm):
    """
    Registers a User plus its StudentProfile atomically.

    Privilege fields (is_staff, is_superuser, ...) are NOT in this form, and
    save() never sets them, so registrants are always plain students.
    """

    student_id = forms.CharField(max_length=30, label="Student ID")
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

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_student_id(self):
        student_id = self.cleaned_data["student_id"].strip()
        if not student_id:
            raise ValidationError("This field is required.")
        if StudentProfile.objects.filter(student_id__iexact=student_id).exists():
            raise ValidationError("A student with this ID already exists.")
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
        StudentProfile.objects.create(
            user=user,
            student_id=self.cleaned_data["student_id"],
            department=self.cleaned_data["department"],
            level=self.cleaned_data["level"],
        )
        return user
