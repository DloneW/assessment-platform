from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from authorization.roles import STUDENT, assign_role

from .models import Course, Department, Level, StudentProfile

User = get_user_model()

STRONG_PASSWORD = "Tr0ub4dor&3-horse"


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name="Cybersecurity Science", code="CYB")
        cls.dept2 = Department.objects.create(name="Computer Science", code="CSC")
        cls.level = Level.objects.create(number=200)
        cls.level2 = Level.objects.create(number=300)

    def registration_data(self, **overrides):
        data = {
            "first_name": "Ada",
            "last_name": "Obi",
            "username": "adaobi",
            "email": "ada@example.com",
            "student_id": "CYB/2024/001",
            "department": self.dept.pk,
            "level": self.level.pk,
            "password1": STRONG_PASSWORD,
            "password2": STRONG_PASSWORD,
        }
        data.update(overrides)
        return data

    def make_student(self, username="adaobi", email="ada@example.com", student_id="CYB/2024/001",
                     first_name="Ada", last_name="Obi", department=None, level=None):
        user = User.objects.create_user(
            username=username, email=email, password=STRONG_PASSWORD,
            first_name=first_name, last_name=last_name,
        )
        assign_role(user, STUDENT)  # same as registration does
        StudentProfile.objects.create(
            user=user, student_id=student_id,
            department=department or self.dept, level=level or self.level,
        )
        return user


class RegistrationTests(BaseTestCase):
    url = reverse("accounts:register")

    def test_01_valid_registration_succeeds(self):
        response = self.client.post(self.url, self.registration_data())
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="adaobi")
        self.assertEqual(user.student_profile.student_id, "CYB/2024/001")
        self.assertNotEqual(user.password, STRONG_PASSWORD)  # hashed
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_02_duplicate_username_fails(self):
        self.make_student()
        response = self.client.post(
            self.url, self.registration_data(email="other@example.com", student_id="CYB/2024/002")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("username", response.context["form"].errors)
        self.assertEqual(User.objects.count(), 1)

    def test_03_duplicate_email_fails(self):
        self.make_student()
        response = self.client.post(
            self.url,
            self.registration_data(username="someoneelse", email="ADA@example.com", student_id="CYB/2024/002"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(User.objects.count(), 1)

    def test_04_duplicate_student_id_fails(self):
        self.make_student()
        response = self.client.post(
            self.url, self.registration_data(username="someoneelse", email="other@example.com")
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("student_id", response.context["form"].errors)
        self.assertEqual(StudentProfile.objects.count(), 1)

    def test_duplicate_student_id_different_case_fails(self):
        self.make_student()
        response = self.client.post(
            self.url,
            self.registration_data(username="someoneelse", email="other@example.com", student_id="cyb/2024/001"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("student_id", response.context["form"].errors)
        self.assertEqual(StudentProfile.objects.count(), 1)

    def test_registration_does_not_enroll_in_courses(self):
        course = Course.objects.create(code="CYB201", title="Intro", department=self.dept, level=self.level)
        # A "courses" value in the POST must be ignored: enrollment never comes from the form.
        self.client.post(self.url, self.registration_data(courses=[course.pk]))
        profile = StudentProfile.objects.get(student_id="CYB/2024/001")
        self.assertEqual(profile.courses.count(), 0)

    def test_05_password_mismatch_fails(self):
        response = self.client.post(self.url, self.registration_data(password2="Different-Pass-99"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("password2", response.context["form"].errors)
        self.assertFalse(User.objects.exists())

    def test_06_weak_password_fails(self):
        for weak in ("password", "12345678", "short1"):
            response = self.client.post(self.url, self.registration_data(password1=weak, password2=weak))
            self.assertEqual(response.status_code, 200, weak)
            self.assertIn("password1", response.context["form"].errors, weak)
        self.assertFalse(User.objects.exists())

    def test_privilege_fields_in_post_are_ignored(self):
        data = self.registration_data(is_staff="on", is_superuser="on", is_active="on")
        self.client.post(self.url, data)
        user = User.objects.get(username="adaobi")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_registration_is_atomic(self):
        # If profile creation fails, the user must not be left behind.
        from unittest import mock
        with mock.patch("accounts.forms.StudentProfile.objects.create", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                self.client.post(self.url, self.registration_data())
        self.assertFalse(User.objects.filter(username="adaobi").exists())


class AuthenticationTests(BaseTestCase):
    def test_07_valid_login_succeeds(self):
        self.make_student()
        response = self.client.post(
            reverse("accounts:login"), {"username": "adaobi", "password": STRONG_PASSWORD}
        )
        self.assertRedirects(response, reverse("accounts:student_dashboard"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_08_invalid_login_fails(self):
        self.make_student()
        response = self.client.post(
            reverse("accounts:login"), {"username": "adaobi", "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_09_logout_works(self):
        user = self.make_student()
        self.client.force_login(user)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)


class DashboardTests(BaseTestCase):
    url = reverse("accounts:student_dashboard")

    def test_10_unauthenticated_cannot_access_dashboard(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={self.url}")

    def test_11_authenticated_student_can_access_dashboard(self):
        self.client.force_login(self.make_student())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_12_dashboard_shows_own_profile_only(self):
        me = self.make_student()
        self.make_student(
            username="bola", email="bola@example.com", student_id="CSC/2023/777",
            first_name="Bola", last_name="Ige", department=self.dept2, level=self.level2,
        )
        self.client.force_login(me)
        response = self.client.get(self.url)
        self.assertContains(response, "Welcome, Ada")
        self.assertContains(response, "CYB/2024/001")
        self.assertContains(response, "Cybersecurity Science")
        self.assertContains(response, "200 Level")
        self.assertContains(response, "ada@example.com")
        # Other student's data must not leak.
        for other in ("Bola", "CSC/2023/777", "bola@example.com", "Computer Science", "300 Level"):
            self.assertNotContains(response, other)

    def test_dashboard_ignores_url_parameters(self):
        me = self.make_student()
        self.make_student(username="bola", email="bola@example.com", student_id="CSC/2023/777",
                          first_name="Bola")
        self.client.force_login(me)
        response = self.client.get(self.url, {"student_id": "CSC/2023/777", "user": "bola"})
        self.assertContains(response, "CYB/2024/001")
        self.assertNotContains(response, "CSC/2023/777")


class MatricNumberTests(BaseTestCase):
    def post(self, matric, username):
        data = self.registration_data(username=username, email=f"{username}@example.com", student_id=matric)
        return self.client.post(reverse("accounts:register"), data)

    def test_each_student_registers_with_their_own_different_matric_number(self):
        for i, matric in enumerate(["AATU/CYS/22/0001", "19-0345", "CSC.2021.77", "ABC_12/3"]):
            response = self.post(matric, f"stud{i}")
            self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(
            sorted(StudentProfile.objects.filter(user__username__startswith="stud").values_list("student_id", flat=True)),
            ["19-0345", "AATU/CYS/22/0001", "ABC_12/3", "CSC.2021.77"],
        )

    def test_matric_number_is_stored_upper_case_and_duplicates_ignore_case(self):
        self.post("  aatu/cys/22/0001 ", "one")
        self.assertTrue(StudentProfile.objects.filter(student_id="AATU/CYS/22/0001").exists())
        response = self.post("Aatu/Cys/22/0001", "two")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username="two").exists())

    def test_unsafe_or_malformed_matric_numbers_are_rejected(self):
        for bad in ["AB", "AATU 22 001", "CYB/<b>x</b>", "'; DROP TABLE--", "/22/001", "X" * 31]:
            response = self.post(bad, "bad")
            self.assertEqual(response.status_code, 200, bad)
            self.assertFalse(get_user_model().objects.filter(username="bad").exists(), bad)

    def test_registration_page_asks_for_matric_number(self):
        self.assertContains(self.client.get(reverse("accounts:register")), "Matric number")


class AdminLoginLandingTests(BaseTestCase):
    """A createsuperuser account holds no application role; logging in must not dead-end at a 403."""

    def test_roleless_superuser_lands_on_admin(self):
        User = get_user_model()
        User.objects.create_superuser("boss", "boss@example.com", "Zq9!longpass-test")
        response = self.client.post(reverse("accounts:login"), {"username": "boss", "password": "Zq9!longpass-test"})
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_roleless_non_staff_still_gets_403_on_dashboard(self):
        User = get_user_model()
        User.objects.create_user("nobody", "nobody@example.com", "Zq9!longpass-test")
        self.client.login(username="nobody", password="Zq9!longpass-test")
        self.assertEqual(self.client.get(reverse("accounts:dashboard")).status_code, 403)

    def test_roleless_superuser_visiting_login_is_sent_to_admin(self):
        User = get_user_model()
        User.objects.create_superuser("boss", "boss@example.com", "Zq9!longpass-test")
        self.client.login(username="boss", password="Zq9!longpass-test")
        response = self.client.get(reverse("accounts:login"))
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)


class ModelTests(BaseTestCase):
    def test_13_student_profile_user_relationship(self):
        user = self.make_student()
        profile = user.student_profile
        self.assertEqual(profile.user, user)
        self.assertEqual(StudentProfile.objects.get(user=user), profile)
        self.assertEqual(profile.department, self.dept)
        self.assertEqual(profile.level, self.level)

    def test_14_unique_constraints(self):
        self.make_student()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentProfile.objects.create(
                user=User.objects.create_user("x", "x@example.com", STRONG_PASSWORD),
                student_id="CYB/2024/001", department=self.dept, level=self.level,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Department.objects.create(name="Other", code="CYB")
        Course.objects.create(code="CYB201", title="Intro", department=self.dept, level=self.level)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Course.objects.create(code="CYB201", title="Dup", department=self.dept, level=self.level)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("y", "ADA@example.com", STRONG_PASSWORD)  # case-insensitive email
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentProfile.objects.create(
                user=User.objects.get(username="adaobi"), student_id="NEW/1",
                department=self.dept, level=self.level,
            )  # one profile per user

    def test_student_id_unique_case_insensitively_at_db_level(self):
        self.make_student()  # student_id "CYB/2024/001"
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentProfile.objects.create(
                user=User.objects.create_user("z", "z@example.com", STRONG_PASSWORD),
                student_id="cyb/2024/001", department=self.dept, level=self.level,
            )

    def test_case_insensitive_constraints_give_readable_validation_errors(self):
        self.make_student()
        other = User.objects.create_user("z", "z@example.com", STRONG_PASSWORD)

        profile = StudentProfile(user=other, student_id="Cyb/2024/001", department=self.dept, level=self.level)
        with self.assertRaises(ValidationError) as cm:
            profile.full_clean()
        self.assertIn("A student with this ID already exists.", cm.exception.messages)

        with self.assertRaises(ValidationError) as cm:
            User(username="y", email="ADA@Example.com", password="x").full_clean()
        self.assertIn("A user with this email already exists.", cm.exception.messages)


class CourseEnrollmentTests(BaseTestCase):
    def setUp(self):
        self.c1 = Course.objects.create(code="CYB201", title="Intro", department=self.dept, level=self.level)
        self.c2 = Course.objects.create(code="CYB202", title="Networks", department=self.dept, level=self.level)

    def test_new_profile_has_no_courses(self):
        profile = self.make_student().student_profile
        self.assertEqual(profile.courses.count(), 0)

    def test_student_can_have_multiple_courses(self):
        profile = self.make_student().student_profile
        profile.courses.add(self.c1, self.c2)
        self.assertCountEqual(profile.courses.all(), [self.c1, self.c2])

    def test_course_has_multiple_students_via_reverse_relation(self):
        p1 = self.make_student().student_profile
        p2 = self.make_student(username="bola", email="bola@example.com", student_id="CYB/2024/002").student_profile
        p1.courses.add(self.c1)
        p2.courses.add(self.c1)
        self.assertCountEqual(self.c1.students.all(), [p1, p2])
        self.assertEqual(self.c2.students.count(), 0)

    def test_same_course_cannot_be_enrolled_twice(self):
        profile = self.make_student().student_profile
        profile.courses.add(self.c1)
        profile.courses.add(self.c1)  # idempotent through the ORM API
        through = StudentProfile.courses.through
        self.assertEqual(through.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            through.objects.create(student=profile, course=self.c1)  # DB-level guard

    def test_deleting_student_removes_enrollments_but_keeps_course(self):
        user = self.make_student()
        user.student_profile.courses.add(self.c1, self.c2)
        user.delete()
        self.assertEqual(StudentProfile.courses.through.objects.count(), 0)
        self.assertEqual(Course.objects.count(), 2)
