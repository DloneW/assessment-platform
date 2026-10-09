"""In-app administration console: access control, behaviour and abuse cases."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from accounts.console_forms import generate_temporary_password
from accounts.models import Course, Department, Level
from authorization.test_boundary import masked
from authorization.roles import ADMIN, LECTURER, STUDENT, holds_role
from authorization.tests import PASSWORD, AuthzTestCase, make_user

User = get_user_model()

CONSOLE_GETS = [
    "console_departments", "console_levels", "console_courses", "console_course_add",
    "console_lecturers", "console_lecturer_add", "console_accounts",
]


def url(name, *args):
    return reverse(f"accounts:{name}", args=args)


class ConsoleAccessTests(AuthzTestCase):
    def object_urls(self):
        return [
            url("console_department_edit", self.cyb.pk),
            url("console_course_edit", self.cyb201.pk),
            url("console_reset_password", self.alice.pk),
        ]

    def test_only_an_application_admin_can_open_the_console(self):
        urls = [url(n) for n in CONSOLE_GETS] + self.object_urls()
        for actor, expected in ((self.alice, 403), (self.lect1, 403), (self.superuser, 403), (self.app_admin, 200)):
            self.client.force_login(actor)
            for u in urls:
                with self.subTest(actor=actor.username, url=u):
                    self.assertEqual(self.client.get(u).status_code, expected)

    def test_anonymous_users_are_sent_to_login(self):
        for u in [url(n) for n in CONSOLE_GETS] + self.object_urls():
            with self.subTest(url=u):
                response = self.client.get(u)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_every_mutation_is_refused_to_non_admins(self):
        posts = [
            (url("console_departments"), {"name": "Physics"}),
            (url("console_department_delete", self.csc.pk), {}),
            (url("console_levels"), {"number": 500}),
            (url("console_course_add"), {"code": "NEW101", "title": "x", "department": self.cyb.pk, "level": self.l200.pk}),
            (url("console_lecturer_add"), {"first_name": "A", "last_name": "B", "username": "newlect", "email": "n@example.com"}),
            (url("console_reset_password", self.alice.pk), {}),
        ]
        for actor in (self.alice, self.lect1, self.superuser):
            self.client.force_login(actor)
            for u, data in posts:
                with self.subTest(actor=actor.username, url=u):
                    self.assertEqual(self.client.post(u, data).status_code, 403)
        self.assertFalse(Department.objects.filter(name="Physics").exists())
        self.assertFalse(User.objects.filter(username="newlect").exists())
        self.assertTrue(Department.objects.filter(pk=self.csc.pk).exists())
        self.assertTrue(self.alice.check_password(PASSWORD))

    def test_mutations_need_a_csrf_token(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.app_admin)
        for u in (url("console_departments"), url("console_department_delete", self.csc.pk),
                  url("console_reset_password", self.alice.pk)):
            self.assertEqual(c.post(u, {"name": "X"}).status_code, 403)
        self.assertTrue(Department.objects.filter(pk=self.csc.pk).exists())
        self.assertTrue(self.alice.check_password(PASSWORD))

    def test_delete_is_post_only(self):
        self.client.force_login(self.app_admin)
        self.assertEqual(self.client.get(url("console_department_delete", self.csc.pk)).status_code, 405)

    def test_login_lands_admin_on_overview_with_links(self):
        response = self.client.post(reverse("accounts:login"), {"username": "appadmin", "password": PASSWORD})
        self.assertRedirects(response, self.admin_dashboard(), fetch_redirect_response=False)
        page = self.client.get(self.admin_dashboard())
        for n in ("console_departments", "console_courses", "console_lecturers", "console_accounts"):
            self.assertContains(page, url(n))


class DepartmentConsoleTests(AuthzTestCase):
    def setUp(self):
        self.client.force_login(self.app_admin)

    def test_department_has_no_code_field(self):
        self.assertNotIn("code", [f.name for f in Department._meta.get_fields()])
        page = self.client.get(url("console_departments"))
        self.assertNotContains(page, 'name="code"')

    def test_add_rename_and_names_are_unique_ignoring_case_and_spacing(self):
        self.client.post(url("console_departments"), {"name": "  Mass   Communication "})
        dept = Department.objects.get(name="Mass Communication")
        for dup in ("mass communication", "MASS  COMMUNICATION", "Cybersecurity science"):
            r = self.client.post(url("console_departments"), {"name": dup})
            self.assertEqual(r.status_code, 200, dup)
        self.assertEqual(Department.objects.filter(name__iexact="mass communication").count(), 1)
        self.client.post(url("console_department_edit", dept.pk), {"name": "Mass Comm"})
        dept.refresh_from_db()
        self.assertEqual(dept.name, "Mass Comm")
        r = self.client.post(url("console_department_edit", dept.pk), {"name": "Computer Science"})
        self.assertEqual(r.status_code, 200)

    def test_department_with_courses_or_students_cannot_be_deleted(self):
        r = self.client.post(url("console_department_delete", self.cyb.pk), follow=True)
        self.assertTrue(Department.objects.filter(pk=self.cyb.pk).exists())
        self.assertContains(r, "cannot be deleted")

    def test_empty_department_can_be_deleted(self):
        empty = Department.objects.create(name="Empty")
        self.client.post(url("console_department_delete", empty.pk))
        self.assertFalse(Department.objects.filter(pk=empty.pk).exists())

    def test_unknown_department_is_404(self):
        self.assertEqual(self.client.get(url("console_department_edit", 999999)).status_code, 404)
        self.assertEqual(self.client.post(url("console_department_delete", 999999)).status_code, 404)

    def test_markup_in_a_name_is_escaped_when_displayed(self):
        self.client.post(url("console_departments"), {"name": "<script>alert(1)</script>"})
        page = self.client.get(url("console_departments"))
        self.assertNotContains(page, "<script>alert(1)</script>")

    def test_level_can_be_added_once(self):
        self.client.post(url("console_levels"), {"number": 400})
        self.assertTrue(Level.objects.filter(number=400).exists())
        self.assertEqual(self.client.post(url("console_levels"), {"number": 400}).status_code, 200)
        self.assertEqual(Level.objects.filter(number=400).count(), 1)


class CourseConsoleTests(AuthzTestCase):
    def setUp(self):
        self.client.force_login(self.app_admin)

    def data(self, **kw):
        base = {"code": "cyb 401", "title": "  Threat   Modelling ", "department": self.cyb.pk, "level": self.l300.pk,
                "is_active": "on", "lecturers": [self.lect1.pk, self.lect2.pk]}
        base.update(kw)
        return base

    def test_create_normalises_and_assigns_lecturers(self):
        r = self.client.post(url("console_course_add"), self.data())
        self.assertRedirects(r, url("console_courses"), fetch_redirect_response=False)
        c = Course.objects.get(code="CYB 401")
        self.assertEqual(c.title, "Threat Modelling")
        self.assertEqual(set(c.lecturers.all()), {self.lect1, self.lect2})

    def test_duplicate_code_ignoring_case_is_rejected(self):
        r = self.client.post(url("console_course_add"), self.data(code="cyb201"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Course.objects.filter(code__iexact="cyb201").count(), 1)

    def test_bad_code_characters_are_rejected(self):
        for bad in ("<b>", "A", "X" * 21, "CYB;DROP"):
            r = self.client.post(url("console_course_add"), self.data(code=bad))
            self.assertEqual(r.status_code, 200, bad)

    def test_only_active_lecturers_can_be_assigned(self):
        for target in (self.alice, self.app_admin, self.superuser):  # a student, an admin, a superuser
            r = self.client.post(url("console_course_add"), self.data(code="NEW1", lecturers=[target.pk]))
            self.assertEqual(r.status_code, 200, target.username)
            self.assertFalse(Course.objects.filter(code="NEW1").exists())
        retired = make_user("retired", LECTURER, is_active=False)
        r = self.client.post(url("console_course_add"), self.data(code="NEW1", lecturers=[retired.pk]))
        self.assertEqual(r.status_code, 200)

    def test_edit_changes_lecturers_and_closes_the_course(self):
        self.client.post(url("console_course_edit", self.cyb205.pk), {
            "code": "CYB205", "title": "Renamed", "department": self.cyb.pk, "level": self.l200.pk,
            "lecturers": [self.lect1.pk],
        })
        self.cyb205.refresh_from_db()
        self.assertEqual(self.cyb205.title, "Renamed")
        self.assertFalse(self.cyb205.is_active)  # unchecked box = closed
        self.assertEqual(list(self.cyb205.lecturers.all()), [self.lect1])

    def test_assigned_lecturer_actually_gains_access(self):
        self.client.post(url("console_course_add"), self.data(lecturers=[self.lect2.pk]))
        course = Course.objects.get(code="CYB 401")
        self.assertEqual(self.get_as(self.lect2, self.course_url(course)).status_code, 200)
        self.assertEqual(self.get_as(self.lect1, self.course_url(course)).status_code, 404)

    def test_course_list_renders(self):
        page = self.client.get(url("console_courses"))
        self.assertContains(page, "CYB201")


class LecturerAndPasswordTests(AuthzTestCase):
    LECT = {"first_name": "Ngozi", "last_name": "Eze", "username": "ngozi", "email": "ngozi@example.com"}

    def create_lecturer(self, **kw):
        self.client.force_login(self.app_admin)
        r = self.client.post(url("console_lecturer_add"), {**self.LECT, **kw})
        return r

    def test_create_lecturer_shows_a_one_time_password_and_sets_flags(self):
        r = self.create_lecturer(is_staff="on", is_superuser="on", groups="1", role="ADMIN")
        self.assertEqual(r.status_code, 200)
        user = User.objects.get(username="ngozi")
        self.assertTrue(holds_role(user, LECTURER))
        self.assertFalse(holds_role(user, ADMIN))
        self.assertFalse(user.is_staff or user.is_superuser)  # forged privilege fields are ignored
        self.assertTrue(user.must_change_password)
        temp = r.context["temporary_password"]
        self.assertContains(r, temp)
        self.assertTrue(user.check_password(temp))
        self.assertNotEqual(user.password, temp)  # hashed
        # Not repeated anywhere afterwards.
        for name in ("console_lecturers", "console_accounts"):
            self.assertNotContains(self.client.get(url(name)), temp)
        self.assertNotIn(temp, str(dict(self.client.session)))

    def test_duplicate_username_or_email_is_rejected(self):
        self.create_lecturer()
        for kw in ({"username": "NGOZI", "email": "other@example.com"}, {"username": "other", "email": "NGOZI@example.com"}):
            r = self.create_lecturer(**kw)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(User.objects.filter(username="other").exists())

    def test_new_lecturer_must_change_password_before_anything_else(self):
        temp = self.create_lecturer().context["temporary_password"]
        lecturer_client = Client()
        r = lecturer_client.post(reverse("accounts:login"), {"username": "ngozi", "password": temp})
        self.assertEqual(r.status_code, 302)
        for u in (self.lecturer_dashboard(), url("dashboard"), url("course_detail", self.cyb201.pk)):
            r = lecturer_client.get(u)
            self.assertRedirects(r, url("password_change"), fetch_redirect_response=False)
        self.assertEqual(lecturer_client.get(url("password_change")).status_code, 200)
        # Same password, weak password and mismatch are all refused; the flag stays.
        for old, new1, new2 in ((temp, temp, temp), (temp, "12345678", "12345678"), (temp, "Fresh-Pass-2026x", "different")):
            r = lecturer_client.post(url("password_change"), {"old_password": old, "new_password1": new1, "new_password2": new2})
            self.assertEqual(r.status_code, 200)
        self.assertTrue(User.objects.get(username="ngozi").must_change_password)
        r = lecturer_client.post(url("password_change"), {
            "old_password": temp, "new_password1": "Fresh-Pass-2026x", "new_password2": "Fresh-Pass-2026x"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.get(username="ngozi").must_change_password)
        self.assertEqual(lecturer_client.get(self.lecturer_dashboard()).status_code, 200)
        # Logout stays reachable while flagged is covered below; the old temp password is dead now.
        self.assertFalse(User.objects.get(username="ngozi").check_password(temp))

    def test_flagged_user_can_still_log_out(self):
        user = make_user("flagged", STUDENT, must_change_password=True)
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse("accounts:logout")).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_reset_password_flow_for_a_student(self):
        victim = Client()
        victim.force_login(self.alice)  # alice has a live session
        self.assertEqual(victim.get(self.student_dashboard()).status_code, 200)
        self.client.force_login(self.app_admin)
        confirm = self.client.get(url("console_reset_password", self.alice.pk))
        self.assertEqual(confirm.status_code, 200)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(PASSWORD))  # GET changes nothing
        r = self.client.post(url("console_reset_password", self.alice.pk))
        temp = r.context["temporary_password"]
        self.assertContains(r, temp)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.check_password(PASSWORD))
        self.assertTrue(self.alice.check_password(temp))
        self.assertTrue(self.alice.must_change_password)
        # Her old session is dead, and the new login is held at the change-password page.
        self.assertEqual(victim.get(self.student_dashboard()).status_code, 302)
        fresh = Client()
        fresh.post(reverse("accounts:login"), {"username": "alice", "password": temp})
        self.assertRedirects(fresh.get(self.student_dashboard()), url("password_change"), fetch_redirect_response=False)

    def test_privileged_and_own_accounts_cannot_be_reset_from_the_console(self):
        staff_lect = make_user("stafflect", LECTURER, is_staff=True)
        for target in (self.app_admin, self.superuser, staff_lect):
            self.client.force_login(self.app_admin)
            for method in (self.client.get, self.client.post):
                self.assertEqual(method(url("console_reset_password", target.pk)).status_code, 404, target.username)
            target.refresh_from_db()
            self.assertTrue(target.check_password(PASSWORD))
        other_admin = make_user("admin2", ADMIN)
        self.assertEqual(self.client.post(url("console_reset_password", other_admin.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("console_reset_password", 999999)).status_code, 404)

    def test_denied_and_missing_reset_targets_look_identical(self):
        a = self.client.force_login(self.app_admin) or self.client.get(url("console_reset_password", self.superuser.pk))
        b = self.client.get(url("console_reset_password", 999999))
        self.assertEqual(masked(a), masked(b))

    def test_account_list_never_offers_privileged_accounts(self):
        self.client.force_login(self.app_admin)
        page = self.client.get(url("console_accounts"))
        self.assertContains(page, "alice")
        self.assertContains(page, "lect1")
        for hidden in ("appadmin", "root"):
            self.assertNotContains(page, f"({hidden},")
        found = self.client.get(url("console_accounts"), {"q": self.alice.student_profile.student_id})
        self.assertContains(found, "alice")
        self.assertNotContains(found, "lect1")

    def test_student_cannot_use_a_reset_url_to_reset_someone_else(self):
        self.client.force_login(self.alice)
        r = self.client.post(url("console_reset_password", self.bob.pk))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self.bob.check_password(PASSWORD))

    def test_ordinary_users_can_change_their_own_password(self):
        self.client.force_login(self.alice)
        r = self.client.post(url("password_change"), {
            "old_password": PASSWORD, "new_password1": "Another-Pass-77q", "new_password2": "Another-Pass-77q"})
        self.assertEqual(r.status_code, 302)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("Another-Pass-77q"))
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)  # session kept

    def test_generated_passwords_are_random_and_valid(self):
        seen = {generate_temporary_password() for _ in range(50)}
        self.assertEqual(len(seen), 50)
        self.assertTrue(all(len(p) == 14 for p in seen))

    def test_console_pages_are_not_cacheable(self):
        self.client.force_login(self.app_admin)
        self.assertIn("no-store", self.client.get(url("console_accounts"))["Cache-Control"])

    def test_groups_are_untouched_by_console_actions(self):
        before = set(Group.objects.values_list("name", flat=True))
        self.create_lecturer()
        self.assertEqual(before, set(Group.objects.values_list("name", flat=True)))


class CreateAppAdminCommandTests(AuthzTestCase):
    def run_cmd(self, **env):
        import os
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command
        out = StringIO()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.stdin.isatty", return_value=False):
            call_command("create_app_admin", stdout=out)
        return out.getvalue()

    ENV = {"DJANGO_APP_ADMIN_USERNAME": "firstadmin", "DJANGO_APP_ADMIN_EMAIL": "first@example.com",
           "DJANGO_APP_ADMIN_PASSWORD": "Temp-Start-Pass-91x"}

    def test_creates_a_non_privileged_admin_who_must_change_password_and_is_idempotent(self):
        self.run_cmd(**self.ENV)
        user = User.objects.get(username="firstadmin")
        self.assertTrue(holds_role(user, ADMIN))
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password(self.ENV["DJANGO_APP_ADMIN_PASSWORD"]))
        out = self.run_cmd(**{**self.ENV, "DJANGO_APP_ADMIN_PASSWORD": "Different-Pass-55y"})
        self.assertIn("already exists", out)
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.ENV["DJANGO_APP_ADMIN_PASSWORD"]))  # never overwritten
        self.assertEqual(User.objects.filter(username__iexact="firstadmin").count(), 1)

    def test_does_nothing_without_details_and_rejects_weak_passwords(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in self.ENV:
                os.environ.pop(k, None)
            self.assertIn("nothing to do", self.run_cmd())
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd(**{**self.ENV, "DJANGO_APP_ADMIN_PASSWORD": "12345678"})
        self.assertFalse(User.objects.filter(username="firstadmin").exists())

    def test_new_admin_logs_in_changes_password_then_reaches_the_console(self):
        self.run_cmd(**self.ENV)
        c = Client()
        c.post(reverse("accounts:login"), {"username": "firstadmin", "password": self.ENV["DJANGO_APP_ADMIN_PASSWORD"]})
        self.assertRedirects(c.get(url("console_departments")), url("password_change"), fetch_redirect_response=False)
        c.post(url("password_change"), {"old_password": self.ENV["DJANGO_APP_ADMIN_PASSWORD"],
                                        "new_password1": "My-Own-Pass-2026z", "new_password2": "My-Own-Pass-2026z"})
        self.assertEqual(c.get(url("console_departments")).status_code, 200)
