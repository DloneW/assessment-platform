"""
Authorization tests.

Each test is named for the rule it protects. Several tests deliberately play the
attacker: they hand-build URLs, change IDs, and submit forged role/privilege fields.
Tests marked with a number correspond to the Day 2 checklist.
"""

import re
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Course, Department, Enrollment, Level, StudentProfile
from authorization import services
from authorization.roles import ADMIN, ALL_ROLES, LECTURER, STUDENT, assign_role, has_role, primary_role

User = get_user_model()
PASSWORD = "Tr0ub4dor&3-horse"

# Fast hasher: these tests are about authorization, not about password hashing strength.
fast_hasher = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


def make_user(username, role=None, **extra):
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=PASSWORD,
        first_name=username.capitalize(),
        last_name="Test",
        **extra,
    )
    if role:
        assign_role(user, role)
    return user


def make_student(username, department, level, courses=()):
    user = make_user(username, STUDENT)
    profile = StudentProfile.objects.create(
        user=user, student_id=f"{department.code}/{username}", department=department, level=level
    )
    for course in courses:
        Enrollment.objects.create(student=profile, course=course)
    return user


@fast_hasher
class AuthzTestCase(TestCase):
    """Shared world: two departments, two levels, courses, students, lecturers, an admin."""

    @classmethod
    def setUpTestData(cls):
        cls.cyb = Department.objects.create(name="Cybersecurity Science", code="CYB")
        cls.csc = Department.objects.create(name="Computer Science", code="CSC")
        cls.l200 = Level.objects.create(number=200)
        cls.l300 = Level.objects.create(number=300)

        def course(code, dept, level, **extra):
            return Course.objects.create(code=code, title=f"{code} title", department=dept, level=level, **extra)

        cls.cyb201 = course("CYB201", cls.cyb, cls.l200)
        cls.cyb203 = course("CYB203", cls.cyb, cls.l200)
        cls.cyb205 = course("CYB205", cls.cyb, cls.l200)  # only bob and lect2 are tied to it
        cls.csc201 = course("CSC201", cls.csc, cls.l200)
        cls.cyb301 = course("CYB301", cls.cyb, cls.l300)
        cls.inactive = course("CYB299", cls.cyb, cls.l200, is_active=False)

        cls.alice = make_student("alice", cls.cyb, cls.l200, [cls.cyb201, cls.cyb203, cls.inactive])
        cls.bob = make_student("bob", cls.cyb, cls.l200, [cls.cyb201, cls.cyb205])
        # Enrolled in CYB201 but with the WRONG department / level: attributes must still deny.
        cls.carol = make_student("carol", cls.csc, cls.l200, [cls.cyb201])
        cls.dave = make_student("dave", cls.cyb, cls.l300, [cls.cyb201])

        cls.lect1 = make_user("lect1", LECTURER)
        cls.lect2 = make_user("lect2", LECTURER)
        for c in (cls.cyb201, cls.cyb203, cls.inactive):
            c.lecturers.add(cls.lect1)
        cls.cyb205.lecturers.add(cls.lect2)

        cls.app_admin = make_user("appadmin", ADMIN)
        cls.superuser = User.objects.create_superuser("root", "root@example.com", PASSWORD)  # no app role

    # helpers
    def get_as(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    @staticmethod
    def student_dashboard():
        return reverse("accounts:student_dashboard")

    @staticmethod
    def lecturer_dashboard():
        return reverse("accounts:lecturer_dashboard")

    @staticmethod
    def admin_dashboard():
        return reverse("accounts:admin_dashboard")

    @staticmethod
    def course_url(course):
        return reverse("accounts:course_detail", args=[course.pk])

    @staticmethod
    def profile_url(user):
        return reverse("accounts:student_profile", args=[user.student_profile.pk])


# --------------------------------------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------------------------------------
class AuthenticationTests(AuthzTestCase):
    def test_01_anonymous_user_cannot_access_any_protected_url(self):
        urls = [
            reverse("accounts:dashboard"),
            self.student_dashboard(),
            self.lecturer_dashboard(),
            self.admin_dashboard(),
            self.course_url(self.cyb201),
            self.profile_url(self.alice),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(
                    response, f"{reverse('accounts:login')}?next={url}", fetch_redirect_response=False
                )

    def test_inactive_user_is_treated_as_anonymous_and_has_no_role(self):
        ghost = make_user("ghost", STUDENT, is_active=False)
        self.assertFalse(has_role(ghost, STUDENT))
        response = self.get_as(ghost, self.student_dashboard())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


# --------------------------------------------------------------------------------
# ROLE AUTHORIZATION
# --------------------------------------------------------------------------------
class RoleAuthorizationTests(AuthzTestCase):
    def test_02_student_cannot_access_lecturer_dashboard(self):
        self.assertEqual(self.get_as(self.alice, self.lecturer_dashboard()).status_code, 403)

    def test_03_student_cannot_access_admin_dashboard(self):
        self.assertEqual(self.get_as(self.alice, self.admin_dashboard()).status_code, 403)

    def test_04_lecturer_cannot_access_admin_dashboard(self):
        self.assertEqual(self.get_as(self.lect1, self.admin_dashboard()).status_code, 403)

    def test_05_admin_can_access_admin_dashboard(self):
        response = self.get_as(self.app_admin, self.admin_dashboard())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Administration")

    def test_roles_are_not_a_hierarchy(self):
        # An application admin is not implicitly a student or lecturer, and vice versa.
        self.assertEqual(self.get_as(self.app_admin, self.student_dashboard()).status_code, 403)
        self.assertEqual(self.get_as(self.app_admin, self.lecturer_dashboard()).status_code, 403)
        self.assertEqual(self.get_as(self.lect1, self.student_dashboard()).status_code, 403)

    def test_django_superuser_without_admin_role_is_not_an_application_admin(self):
        self.assertEqual(self.get_as(self.superuser, self.admin_dashboard()).status_code, 403)
        self.assertEqual(self.get_as(self.superuser, self.student_dashboard()).status_code, 403)

    def test_application_admin_is_not_django_staff_or_superuser(self):
        self.assertFalse(self.app_admin.is_staff)
        self.assertFalse(self.app_admin.is_superuser)
        response = self.get_as(self.app_admin, reverse("admin:index"))  # Django admin site
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_removing_a_role_takes_effect_on_the_next_request(self):
        self.client.force_login(self.app_admin)
        self.assertEqual(self.client.get(self.admin_dashboard()).status_code, 200)
        self.app_admin.groups.clear()
        self.assertEqual(self.client.get(self.admin_dashboard()).status_code, 403)

    def test_user_with_no_role_is_denied_everywhere(self):
        nobody = make_user("nobody")
        for url in (reverse("accounts:dashboard"), self.student_dashboard(), self.lecturer_dashboard(), self.admin_dashboard()):
            with self.subTest(url=url):
                self.assertEqual(self.get_as(nobody, url).status_code, 403)

    def test_denial_response_does_not_explain_why(self):
        response = self.get_as(self.alice, self.admin_dashboard())
        self.assertEqual(response.status_code, 403)
        for leak in ("ADMIN", "role", "group", "STUDENT"):
            self.assertNotContains(response, leak, status_code=403)


# --------------------------------------------------------------------------------
# ATTRIBUTE AUTHORIZATION (department, level, enrollment, course state)
# --------------------------------------------------------------------------------
class AttributeAuthorizationTests(AuthzTestCase):
    def test_06_student_can_access_enrolled_course(self):
        self.assertTrue(services.can_access_course(self.alice, self.cyb201))
        response = self.get_as(self.alice, self.course_url(self.cyb201))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CYB201")

    def test_07_student_cannot_access_unenrolled_course(self):
        # Same department and level as CYB205, but alice is not enrolled in it.
        self.assertTrue(services.can_take_course(self.alice, self.cyb205))
        self.assertFalse(services.can_access_course(self.alice, self.cyb205))
        self.assertEqual(self.get_as(self.alice, self.course_url(self.cyb205)).status_code, 404)

    def test_08_student_from_wrong_department_cannot_access_restricted_course(self):
        # carol IS enrolled in CYB201, but she belongs to Computer Science.
        self.assertTrue(Enrollment.objects.filter(student=self.carol.student_profile, course=self.cyb201).exists())
        self.assertFalse(services.can_take_course(self.carol, self.cyb201))
        self.assertFalse(services.can_access_course(self.carol, self.cyb201))
        self.assertEqual(self.get_as(self.carol, self.course_url(self.cyb201)).status_code, 404)

    def test_09_student_from_wrong_level_cannot_access_restricted_course(self):
        # dave IS enrolled in CYB201 (a 200-level course), but he is a 300-level student.
        self.assertTrue(Enrollment.objects.filter(student=self.dave.student_profile, course=self.cyb201).exists())
        self.assertFalse(services.can_take_course(self.dave, self.cyb201))
        self.assertFalse(services.can_access_course(self.dave, self.cyb201))
        self.assertEqual(self.get_as(self.dave, self.course_url(self.cyb201)).status_code, 404)

    def test_attributes_come_from_the_database_not_from_names_or_codes(self):
        # Moving alice to another department flips the decision with no code change.
        profile = self.alice.student_profile
        self.assertTrue(services.can_access_course(self.alice, self.cyb201))
        profile.department = self.csc
        profile.save()
        self.assertFalse(services.can_access_course(User.objects.get(pk=self.alice.pk), self.cyb201))

    def test_inactive_course_is_denied_to_students_but_open_to_its_lecturer(self):
        self.assertTrue(Enrollment.objects.filter(student=self.alice.student_profile, course=self.inactive).exists())
        self.assertFalse(services.can_access_course(self.alice, self.inactive))
        self.assertEqual(self.get_as(self.alice, self.course_url(self.inactive)).status_code, 404)
        self.assertEqual(self.get_as(self.lect1, self.course_url(self.inactive)).status_code, 200)

    def test_student_role_without_a_profile_cannot_access_courses(self):
        profileless = make_user("profileless", STUDENT)
        self.assertFalse(services.can_take_course(profileless, self.cyb201))
        self.assertFalse(services.can_access_course(profileless, self.cyb201))

    def test_dashboard_lists_only_courses_the_student_may_access(self):
        response = self.get_as(self.alice, self.student_dashboard())
        self.assertContains(response, "CYB201")
        self.assertContains(response, "CYB203")
        self.assertNotContains(response, "CYB205")  # not enrolled
        self.assertNotContains(response, "CYB299")  # inactive


# --------------------------------------------------------------------------------
# OBJECT-LEVEL AUTHORIZATION (ownership / management)
# --------------------------------------------------------------------------------
class ObjectAuthorizationTests(AuthzTestCase):
    def test_10_student_can_access_own_profile(self):
        response = self.get_as(self.alice, self.profile_url(self.alice))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alice@example.com")

    def test_11_student_cannot_access_another_students_profile(self):
        self.client.force_login(self.alice)
        other = self.client.get(self.profile_url(self.bob))
        missing = self.client.get(reverse("accounts:student_profile", args=[999999]))
        self.assertEqual(other.status_code, 404)
        # Indistinguishable from an ID that does not exist: no enumeration.
        self.assertEqual(missing.status_code, 404)
        # Compare bodies with the per-render CSRF token masked; nothing else may differ.
        mask = lambda response: re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", response.content)  # noqa: E731
        self.assertEqual(mask(other), mask(missing))
        self.assertNotContains(other, "bob@example.com", status_code=404)

    def test_11b_changing_the_id_in_the_url_never_reveals_other_profiles(self):
        self.client.force_login(self.alice)
        own_pk = self.alice.student_profile.pk
        for profile in StudentProfile.objects.exclude(pk=own_pk):
            with self.subTest(profile=profile.pk):
                response = self.client.get(reverse("accounts:student_profile", args=[profile.pk]))
                self.assertEqual(response.status_code, 404)

    def test_12_student_cannot_access_another_students_protected_resource(self):
        # CYB205 is bob's course. Alice guessing its ID gets nothing.
        response = self.get_as(self.alice, self.course_url(self.cyb205))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "CYB205", status_code=404)

    def test_dashboard_ignores_ids_supplied_in_the_query_string(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            self.student_dashboard(), {"student_id": self.bob.student_profile.student_id, "user": self.bob.pk}
        )
        self.assertContains(response, "alice@example.com")
        self.assertNotContains(response, "bob@example.com")

    def test_lecturer_cannot_view_student_profiles(self):
        self.assertEqual(self.get_as(self.lect1, self.profile_url(self.alice)).status_code, 404)

    def test_admin_can_view_any_student_profile(self):
        self.assertEqual(self.get_as(self.app_admin, self.profile_url(self.alice)).status_code, 200)

    def test_lecturer_can_manage_only_assigned_courses(self):
        self.assertTrue(services.can_manage_course(self.lect1, self.cyb201))
        self.assertFalse(services.can_manage_course(self.lect1, self.cyb205))  # lect2's course
        self.assertEqual(self.get_as(self.lect1, self.course_url(self.cyb201)).status_code, 200)
        # lect1 changing the ID to lect2's course:
        self.assertEqual(self.get_as(self.lect1, self.course_url(self.cyb205)).status_code, 404)
        self.assertEqual(self.get_as(self.lect2, self.course_url(self.cyb205)).status_code, 200)

    def test_admin_can_manage_and_open_every_course(self):
        for course in Course.objects.all():
            with self.subTest(course=course.code):
                self.assertTrue(services.can_manage_course(self.app_admin, course))
                self.assertEqual(self.get_as(self.app_admin, self.course_url(course)).status_code, 200)

    def test_student_can_never_manage_a_course(self):
        for course in Course.objects.all():
            self.assertFalse(services.can_manage_course(self.alice, course))

    def test_being_listed_as_lecturer_is_not_enough_without_the_lecturer_role(self):
        # limit_choices_to only filters forms; the authorization layer re-checks the role.
        self.cyb201.lecturers.add(self.alice)
        self.assertFalse(services.can_manage_course(User.objects.get(pk=self.alice.pk), self.cyb201))
        self.assertEqual(self.get_as(self.alice, self.lecturer_dashboard()).status_code, 403)


# --------------------------------------------------------------------------------
# ROLE ESCALATION
# --------------------------------------------------------------------------------
class RoleEscalationTests(AuthzTestCase):
    def register(self, **forged):
        """Register through the public form while adding forged privilege fields."""
        data = {
            "first_name": "Eve",
            "last_name": "Mallory",
            "username": "eve",
            "email": "eve@example.com",
            "student_id": "CYB/eve",
            "department": self.cyb.pk,
            "level": self.l200.pk,
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        data.update(forged)
        response = self.client.post(reverse("accounts:register"), data)
        # Registration itself must SUCCEED: the forged fields are ignored, not just rejected.
        self.assertRedirects(response, reverse("accounts:login"))
        return User.objects.get(username="eve")

    def assertOnlyStudent(self, user):
        self.assertEqual(list(user.groups.values_list("name", flat=True)), [STUDENT])
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_13_registration_cannot_create_a_lecturer(self):
        lecturer_group = Group.objects.get(name=LECTURER)
        user = self.register(role="LECTURER", user_type="lecturer", groups=[lecturer_group.pk])
        self.assertOnlyStudent(user)
        self.assertFalse(has_role(user, LECTURER))

    def test_14_registration_cannot_create_an_admin(self):
        admin_group = Group.objects.get(name=ADMIN)
        user = self.register(role="ADMIN", is_admin="true", groups=[admin_group.pk])
        self.assertOnlyStudent(user)
        self.assertFalse(has_role(user, ADMIN))

    def test_15_registration_cannot_create_a_superuser(self):
        user = self.register(is_superuser="on", is_staff="on", role="SUPERUSER", is_active="on")
        self.assertOnlyStudent(user)

    def test_registered_student_really_is_a_student(self):
        user = self.register()
        self.assertTrue(has_role(user, STUDENT))
        self.assertEqual(primary_role(user), STUDENT)

    def test_16_student_cannot_change_own_role_through_http(self):
        admin_group = Group.objects.get(name=ADMIN)
        self.client.force_login(self.alice)

        # (a) Forged fields posted to pages the student CAN reach are ignored.
        forged = {"role": "ADMIN", "is_staff": "true", "is_superuser": "true", "groups": [admin_group.pk]}
        for url in (self.student_dashboard(), self.profile_url(self.alice), self.course_url(self.cyb201)):
            with self.subTest(url=url):
                self.client.post(url, forged)

        # (b) The real role-changing endpoint, Django admin, is closed to non-staff.
        change_url = reverse("admin:accounts_user_change", args=[self.alice.pk])
        response = self.client.post(change_url, {"username": "alice", "groups": [admin_group.pk], "is_superuser": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

        alice = User.objects.get(pk=self.alice.pk)
        self.assertEqual(list(alice.groups.values_list("name", flat=True)), [STUDENT])
        self.assertFalse(alice.is_staff or alice.is_superuser)
        # ...and the privileged pages are still closed.
        self.assertEqual(self.client.get(self.admin_dashboard()).status_code, 403)

    def _admin_change_payload(self, target, **overrides):
        joined = timezone.localtime(target.date_joined)
        data = {
            "username": target.username,
            "email": target.email,
            "first_name": target.first_name,
            "last_name": target.last_name,
            "is_active": "on",
            "date_joined_0": joined.strftime("%Y-%m-%d"),
            "date_joined_1": joined.strftime("%H:%M:%S"),
        }
        data.update(overrides)
        return data

    def test_staff_without_superuser_cannot_grant_roles_in_django_admin(self):
        admin_group = Group.objects.get(name=ADMIN)
        staffer = make_user("staffer", is_staff=True)
        staffer.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="accounts", codename__in=["view_user", "change_user"])
        )
        victim_control = make_user("control", STUDENT)
        victim = make_user("victim", STUDENT)
        forged = {"groups": [admin_group.pk], "is_staff": "on", "is_superuser": "on"}

        # Control: the very same POST DOES work for a superuser, so the next assertion is meaningful.
        self.client.force_login(self.superuser)
        url = reverse("admin:accounts_user_change", args=[victim_control.pk])
        response = self.client.post(url, self._admin_change_payload(victim_control, **forged))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(has_role(User.objects.get(pk=victim_control.pk), ADMIN))

        # A staff member who merely has "change user" permission cannot.
        self.client.force_login(staffer)
        url = reverse("admin:accounts_user_change", args=[victim.pk])
        self.client.post(url, self._admin_change_payload(victim, **forged))
        victim = User.objects.get(pk=victim.pk)
        self.assertEqual(list(victim.groups.values_list("name", flat=True)), [STUDENT])
        self.assertFalse(victim.is_staff or victim.is_superuser)


# --------------------------------------------------------------------------------
# LECTURER AUTHORIZATION
# --------------------------------------------------------------------------------
class LecturerAuthorizationTests(AuthzTestCase):
    def test_17_lecturer_can_access_lecturer_dashboard(self):
        response = self.get_as(self.lect1, self.lecturer_dashboard())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CYB201")
        self.assertNotContains(response, "CYB205")  # lect2's course is not listed

    def test_18_lecturer_cannot_access_admin_only_functions(self):
        self.assertFalse(services.can_access_admin_dashboard(self.lect1))
        self.assertEqual(self.get_as(self.lect1, self.admin_dashboard()).status_code, 403)
        response = self.client.get(reverse("admin:index"))  # Django admin site
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_lecturer_is_not_a_django_superuser_or_staff(self):
        self.assertFalse(self.lect1.is_staff)
        self.assertFalse(self.lect1.is_superuser)

    def test_lecturer_cannot_edit_users_or_roles_via_django_admin(self):
        admin_group = Group.objects.get(name=ADMIN)
        self.client.force_login(self.lect1)
        url = reverse("admin:accounts_user_change", args=[self.lect1.pk])
        response = self.client.post(url, {"username": "lect1", "groups": [admin_group.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(has_role(User.objects.get(pk=self.lect1.pk), ADMIN))


# --------------------------------------------------------------------------------
# ADMIN
# --------------------------------------------------------------------------------
class AdminAuthorizationTests(AuthzTestCase):
    def test_19_authorized_admin_can_access_admin_dashboard(self):
        response = self.get_as(self.app_admin, self.admin_dashboard())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["counts"]["students"], 4)
        self.assertEqual(response.context["counts"]["lecturers"], 2)

    def test_application_dashboard_url_is_not_swallowed_by_django_admin(self):
        # /admin/dashboard/ must reach OUR view; /admin/ must still be Django admin.
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertEqual(self.client.get("/admin/dashboard/").status_code, 403)  # superuser lacks the app role


# --------------------------------------------------------------------------------
# LOGIN LANDING + ROLE HELPERS + MANAGEMENT COMMAND
# --------------------------------------------------------------------------------
class LoginRedirectTests(AuthzTestCase):
    def login(self, user):
        self.client.logout()
        return self.client.post(reverse("accounts:login"), {"username": user.username, "password": PASSWORD})

    def test_each_role_lands_on_its_own_dashboard(self):
        self.assertRedirects(self.login(self.alice), self.student_dashboard())
        self.assertRedirects(self.login(self.lect1), self.lecturer_dashboard())
        self.assertRedirects(self.login(self.app_admin), self.admin_dashboard())

    def test_dashboard_router_sends_each_role_to_its_dashboard(self):
        router = reverse("accounts:dashboard")
        self.assertRedirects(self.get_as(self.alice, router), self.student_dashboard())
        self.assertRedirects(self.get_as(self.lect1, router), self.lecturer_dashboard())
        self.assertRedirects(self.get_as(self.app_admin, router), self.admin_dashboard())

    def test_user_with_no_role_lands_on_a_denial_not_a_loop(self):
        nobody = make_user("nobody")
        response = self.login(nobody)
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(response.url).status_code, 403)

    def test_login_does_not_redirect_to_other_sites(self):
        response = self.client.post(
            reverse("accounts:login") + "?next=https://evil.example/", {"username": "alice", "password": PASSWORD}
        )
        self.assertRedirects(response, self.student_dashboard())


class RoleHelperTests(AuthzTestCase):
    def test_assign_role_replaces_the_other_application_role_only(self):
        other = Group.objects.create(name="Library staff")  # unrelated group must survive
        user = make_user("mover", STUDENT)
        user.groups.add(other)
        assign_role(user, LECTURER)
        self.assertEqual(
            sorted(user.groups.values_list("name", flat=True)), sorted(["Library staff", LECTURER])
        )

    def test_assign_role_rejects_unknown_roles(self):
        for bad in ("SUPERUSER", "admin", "", None):
            with self.subTest(role=bad), self.assertRaises(ValueError):
                assign_role(self.alice, bad)

    def test_primary_role_prefers_the_highest_role(self):
        user = make_user("multi", STUDENT)
        user.groups.add(Group.objects.get(name=ADMIN))
        self.assertEqual(primary_role(user), ADMIN)

    def test_all_three_role_groups_exist(self):
        self.assertEqual(set(Group.objects.filter(name__in=ALL_ROLES).values_list("name", flat=True)), set(ALL_ROLES))


class AssignRoleCommandTests(AuthzTestCase):
    def run_command(self, *args):
        call_command("assign_role", *args, stdout=StringIO())

    def test_command_assigns_a_role_without_touching_django_privileges(self):
        user = make_user("promotee", STUDENT)
        self.run_command("promotee", "lecturer")  # case-insensitive
        user = User.objects.get(pk=user.pk)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), [LECTURER])
        self.assertFalse(user.is_staff or user.is_superuser)

    def test_command_rejects_unknown_user_and_unknown_role(self):
        with self.assertRaises(CommandError):
            self.run_command("no-such-user", "ADMIN")
        with self.assertRaises(CommandError):
            self.run_command("alice", "SUPERUSER")
        self.assertEqual(list(self.alice.groups.values_list("name", flat=True)), [STUDENT])
