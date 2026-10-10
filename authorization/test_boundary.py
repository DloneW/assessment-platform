"""
Day 2 hardening: regression tests for the authorization boundary.

These complement authorization/tests.py (which covers the happy paths and the first
round of attacks). Each test here names the boundary it protects, and its setup shows
the attack: user A, a resource that belongs to someone else, a request by A, and the
expected denial. Where a "control" is needed to prove a denial happened for the right
reason (the same action succeeding for someone who IS allowed), the test includes it.
"""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import URLResolver, get_resolver, reverse
from django.utils import timezone

from accounts.models import Course, Enrollment, StudentProfile
from authorization import services
from authorization.roles import ADMIN, ALL_ROLES, LECTURER, STUDENT
from authorization.tests import PASSWORD, AuthzTestCase, make_student, make_user

User = get_user_model()

ROLE_FORGERY = {
    "role": "ADMIN",
    "group": "ADMIN",
    "is_staff": "true",
    "is_superuser": "true",
    "user_permissions": "1",
}

# Every URL name in the "accounts" namespace and the deliberate decision made for it.
# A new route that is not listed here fails test_every_route_has_a_deliberate_policy.
ROUTE_POLICY = {
    "register": "public",
    "login": "public",
    "logout": "POST only",
    "dashboard": "logged in + holds any role (else 403); GET/HEAD only",
    "student_dashboard": "STUDENT; GET/HEAD only",
    "lecturer_dashboard": "LECTURER; GET/HEAD only",
    "admin_dashboard": "application ADMIN; GET/HEAD only",
    "student_profile": "owner or ADMIN, else 404; GET/HEAD only",
    "course_detail": "can_access_course, else 404; GET/HEAD only",
    "password_change": "logged in (any role); GET/POST; clears the temporary-password flag",
    # In-app administration console: can_manage_directory (application ADMIN) for all of these.
    "console_departments": "application ADMIN; GET/POST",
    "console_department_edit": "application ADMIN; GET/POST; missing id 404",
    "console_department_delete": "POST only; application ADMIN; refused while courses/students exist",
    "console_levels": "application ADMIN; GET/POST",
    "console_courses": "application ADMIN; GET/HEAD only",
    "console_course_add": "application ADMIN; GET/POST",
    "console_course_edit": "application ADMIN; GET/POST; missing id 404",
    "console_lecturers": "application ADMIN; GET/HEAD only",
    "console_lecturer_add": "application ADMIN; GET/POST; creates LECTURER with a one-time temporary password",
    "console_accounts": "application ADMIN; GET/HEAD only; lists only resettable accounts",
    "console_reset_password": "application ADMIN + can_reset_password (never self/staff/superuser/ADMIN), else 404; GET/POST",
}
OBJECT_ID_ROUTES = {"console_department_edit", "console_department_delete", "console_course_edit", "console_reset_password"}
# Every URL name in the "assessments" namespace: (allowed method, who, what a denied object gets).
ASSESSMENT_ROUTE_POLICY = {
    "test_create": ("GET/POST", "can_create_test on the course in the URL, else 404"),
    "test_manage": ("GET", "can_manage_test, else 404"),
    "test_edit": ("GET/POST", "can_manage_test, else 404; draft only"),
    "question_add": ("GET/POST", "can_manage_test, else 404; draft only"),
    "question_edit": ("GET/POST", "can_manage_test + question belongs to test, else 404; draft only"),
    "question_delete": ("POST", "can_manage_test + question belongs to test, else 404; draft only"),
    "test_publish": ("POST", "can_manage_test, else 404; validated"),
    "test_archive": ("POST", "can_manage_test, else 404"),
    "test_intro": ("GET", "can_take_test, else 404"),
    "attempt_start": ("POST", "can_take_test, else 404; can_start_attempt"),
    "attempt_detail": ("GET", "can_view_attempt (owner) + attempt belongs to test in URL, else 404"),
    "attempt_save": ("POST", "can_view_attempt, can_answer_attempt"),
    "attempt_submit": ("POST", "can_view_attempt, can_submit_attempt"),
    "submissions": ("GET", "can_manage_test, else 404"),
    "grade": ("GET/POST", "can_manage_test + can_grade_attempt (finished attempts only), else 404"),
    "answer_file": ("GET", "can_download_answer_file (owner or course manager) + file belongs to attempt, else 404"),
}
READ_ONLY_ROUTES = [name for name, policy in ROUTE_POLICY.items() if "GET/HEAD only" in policy]


def masked(response):
    """Status, content type and body with the per-render CSRF token removed."""
    body = re.sub(rb'name="csrfmiddlewaretoken" value="[^"]+"', b"", response.content)
    return response.status_code, response["Content-Type"], body


def role_snapshot():
    """Everything that makes a user privileged, for every user."""
    return {
        u.pk: (
            u.is_staff,
            u.is_superuser,
            u.is_active,
            tuple(sorted(u.groups.values_list("name", flat=True))),
            tuple(sorted(u.user_permissions.values_list("pk", flat=True))),
        )
        for u in User.objects.all()
    }


def walk(patterns, namespace=None):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from walk(p.url_patterns, p.namespace or namespace)
        else:
            yield namespace, p.name, str(p.pattern)


class BoundaryTestCase(AuthzTestCase):
    def url_for(self, name):
        """A concrete URL for each application route (object routes use bob's objects)."""
        if name == "student_profile":
            return self.profile_url(self.bob)
        if name == "course_detail":
            return self.course_url(self.cyb205)
        if name in OBJECT_ID_ROUTES:
            return reverse(f"accounts:{name}", args=[self.alice.pk])
        return reverse(f"accounts:{name}")

    def login_as(self, user):
        self.client.logout()
        if user is not None:
            self.client.force_login(user)


# --------------------------------------------------------------------------------
# URL-LEVEL BYPASS: every route has a deliberate decision
# --------------------------------------------------------------------------------
class RouteInventoryTests(BoundaryTestCase):
    def test_every_route_has_a_deliberate_policy(self):
        routes = list(walk(get_resolver().url_patterns))
        namespaces = {ns for ns, _, _ in routes}
        self.assertEqual(namespaces, {"accounts", "admin", "assessments", None})  # nothing else is mounted
        self.assertEqual({(n, p) for ns, n, p in routes if ns is None}, {(None, "")})  # root redirect only
        accounts_names = {name for ns, name, _ in routes if ns == "accounts"}
        self.assertEqual(accounts_names, set(ROUTE_POLICY))
        assessment_names = {name for ns, name, _ in routes if ns == "assessments"}
        self.assertEqual(assessment_names, set(ASSESSMENT_ROUTE_POLICY))

    def test_assessment_routes_are_login_gated_and_method_gated(self):
        from django.urls import NoReverseMatch  # noqa: F401  (documenting the failure mode)

        args = {
            "test_create": [self.cyb201.pk], "test_manage": [1], "test_edit": [1], "question_add": [1],
            "question_edit": [1, 1], "question_delete": [1, 1], "test_publish": [1], "test_archive": [1],
            "test_intro": [1], "attempt_start": [1], "attempt_detail": [1, 1], "attempt_save": [1, 1], "attempt_submit": [1, 1],
            "submissions": [1], "grade": [1, 1], "answer_file": [1, 1, 1],
        }
        self.client.logout()
        for name, (methods, _policy) in ASSESSMENT_ROUTE_POLICY.items():
            url = reverse(f"assessments:{name}", args=args[name])
            allowed = methods.split("/")
            with self.subTest(route=name):
                for method in allowed:
                    response = getattr(self.client, method.lower())(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith(reverse("accounts:login") + "?next="))
                for method in {"GET", "POST", "PUT", "PATCH", "DELETE"} - set(allowed):
                    self.assertEqual(getattr(self.client, method.lower())(url).status_code, 405, method)

    def test_no_route_exposes_enrollments_or_role_management(self):
        # Enrollment objects and group membership are reachable only through Django admin
        # (is_staff + model permissions); there is no application route for either.
        for ns, name, pattern in walk(get_resolver().url_patterns):
            if ns == "accounts":
                self.assertNotRegex(pattern, r"enrol|role|group|permission", msg=f"{name}: {pattern}")

    def test_anonymous_users_never_reach_protected_routes(self):
        self.client.logout()
        for name, policy in ROUTE_POLICY.items():
            if policy == "public" or policy.startswith("POST only"):
                continue
            with self.subTest(route=name):
                response = self.client.get(self.url_for(name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("accounts:login") + "?next="))

    def test_django_admin_site_is_closed_to_everyone_who_is_not_staff(self):
        urls = [
            reverse("admin:index"),
            reverse("admin:accounts_user_changelist"),
            reverse("admin:accounts_user_change", args=[self.alice.pk]),
            reverse("admin:auth_user_password_change", args=[self.alice.pk]),
            reverse("admin:accounts_user_delete", args=[self.alice.pk]),
            reverse("admin:accounts_enrollment_changelist"),
            reverse("admin:accounts_studentprofile_changelist"),
            reverse("admin:accounts_course_changelist"),
            reverse("admin:auth_group_changelist"),
        ]
        # An application ADMIN and a LECTURER are NOT Django staff, so the admin site stays closed.
        for actor in (None, self.alice, self.lect1, self.app_admin):
            self.login_as(actor)
            for url in urls:
                with self.subTest(actor=getattr(actor, "username", "anonymous"), url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("/admin/login/", response.url)


# --------------------------------------------------------------------------------
# HTTP METHOD BYPASS
# --------------------------------------------------------------------------------
class HttpMethodTests(BoundaryTestCase):
    UNSAFE = ("POST", "PUT", "PATCH", "DELETE")

    def test_read_only_routes_refuse_unsafe_methods_for_everyone(self):
        """Nothing at these URLs is meant to be modified, so nobody can use another verb on them.

        The refusal is the same 405 for every actor and every object, which also means a
        probing verb cannot reveal whether an object exists (that would turn 404 vs 405 into
        an oracle).
        """
        for actor in (None, self.alice, self.lect1, self.app_admin):
            self.login_as(actor)
            for name in READ_ONLY_ROUTES:
                for method in self.UNSAFE:
                    with self.subTest(actor=getattr(actor, "username", "anonymous"), route=name, method=method):
                        response = self.client.generic(
                            method, self.url_for(name), "role=ADMIN", content_type="application/x-www-form-urlencoded"
                        )
                        self.assertEqual(response.status_code, 405)
                        self.assertEqual(response["Allow"], "GET, HEAD")

    def test_unsafe_verbs_cannot_probe_for_object_existence(self):
        self.client.force_login(self.alice)
        seen = set()
        for pk in (self.alice.student_profile.pk, self.bob.student_profile.pk, 999999):
            for method in self.UNSAFE:
                response = self.client.generic(method, reverse("accounts:student_profile", args=[pk]))
                seen.add((response.status_code, response["Allow"], response.content))
        self.assertEqual(len(seen), 1)  # own, someone else's and missing are indistinguishable

    def test_safe_methods_still_work(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)
        self.assertEqual(self.client.head(self.student_dashboard()).status_code, 200)
        # HEAD must not be a way around the object check either.
        self.assertEqual(self.client.head(self.profile_url(self.bob)).status_code, 404)

    def test_login_and_registration_accept_only_their_intended_methods(self):
        for url in (reverse("accounts:login"), reverse("accounts:register")):
            for method in ("PUT", "PATCH", "DELETE"):
                with self.subTest(url=url, method=method):
                    response = self.client.generic(method, url, "username=alice&password=x")
                    self.assertEqual(response.status_code, 405)

    def test_logout_is_post_only_and_get_does_not_log_out(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)  # still logged in


# --------------------------------------------------------------------------------
# OBJECT-LEVEL: denied objects are indistinguishable from missing ones
# --------------------------------------------------------------------------------
class ObjectExistenceTests(BoundaryTestCase):
    def test_denied_objects_look_exactly_like_missing_objects(self):
        # (attacker, route, object pk, someone who IS allowed to open it, why the attacker is denied)
        cases = [
            (self.alice, "student_profile", self.bob.student_profile.pk, self.bob, "another student's profile"),
            (self.alice, "course_detail", self.cyb205.pk, self.bob, "not enrolled"),
            (self.carol, "course_detail", self.cyb201.pk, self.alice, "enrolled but wrong department"),
            (self.dave, "course_detail", self.cyb201.pk, self.alice, "enrolled but wrong level"),
            (self.alice, "course_detail", self.inactive.pk, self.lect1, "enrolled but course inactive"),
            (self.lect1, "course_detail", self.cyb205.pk, self.lect2, "lecturer not assigned"),
            (self.lect1, "student_profile", self.alice.student_profile.pk, self.app_admin, "lecturer vs student profile"),
            (self.app_admin, "student_profile", 999999, None, "missing (control)"),
        ]
        for attacker, route, pk, rightful, why in cases:
            with self.subTest(why=why):
                url = reverse(f"accounts:{route}", args=[pk])
                if rightful is not None:  # the object really exists and is reachable by its rightful accessor
                    self.assertEqual(self.get_as(rightful, url).status_code, 200)
                self.client.force_login(attacker)
                denied = masked(self.client.get(url))
                missing = masked(self.client.get(reverse(f"accounts:{route}", args=[999999])))
                self.assertEqual(denied[0], 404)
                self.assertEqual(denied, missing)

    def test_own_profile_lists_only_own_enrollments(self):
        # alice is enrolled in CYB201/CYB203, bob in CYB201/CYB205.
        response = self.get_as(self.alice, self.profile_url(self.alice))
        self.assertContains(response, "CYB203")
        self.assertNotContains(response, "CYB205")


# --------------------------------------------------------------------------------
# COURSE ACCESS MATRIX (mirrors the documented rule table)
# --------------------------------------------------------------------------------
class CourseAccessMatrixTests(BoundaryTestCase):
    def test_course_access_matrix(self):
        # kind, department correct, level correct, enrolled, course active, expected
        rows = [
            ("student", True, True, True, True, True),
            ("student", True, True, False, True, False),  # enrollment is required
            ("student", False, True, True, True, False),
            ("student", True, False, True, True, False),
            ("student", True, True, True, False, False),
            ("lecturer-assigned", None, None, None, True, True),
            ("lecturer-assigned", None, None, None, False, True),  # lecturers can still manage an inactive course
            ("lecturer-unassigned", None, None, None, True, False),
            ("lecturer-unassigned", None, None, None, False, False),
            ("admin", None, None, None, True, True),
            ("admin", None, None, None, False, True),
        ]
        for i, (kind, dept_ok, level_ok, enrolled, active, expected) in enumerate(rows):
            with self.subTest(row=rows[i]):
                course = Course.objects.create(
                    code=f"MX{i}", title="matrix", department=self.cyb, level=self.l200, is_active=active
                )
                if kind == "student":
                    user = make_student(
                        f"mx{i}",
                        self.cyb if dept_ok else self.csc,
                        self.l200 if level_ok else self.l300,
                        [course] if enrolled else [],
                    )
                elif kind.startswith("lecturer"):
                    user = make_user(f"mx{i}", LECTURER)
                    if kind == "lecturer-assigned":
                        course.lecturers.add(user)
                else:
                    user = make_user(f"mx{i}", ADMIN)
                self.assertEqual(services.can_access_course(user, course), expected)
                self.assertEqual(self.get_as(user, self.course_url(course)).status_code, 200 if expected else 404)


# --------------------------------------------------------------------------------
# PRIVILEGE ESCALATION: no application route changes anyone's role or privileges
# --------------------------------------------------------------------------------
class NoRouteChangesRolesTests(BoundaryTestCase):
    def test_forged_privilege_data_changes_nothing_on_any_route(self):
        admin_group = Group.objects.get(name=ADMIN)
        body = {**ROLE_FORGERY, "groups": [admin_group.pk]}
        before = role_snapshot()
        for actor in (None, self.alice, self.bob, self.lect1, self.lect2):
            for name in ROUTE_POLICY:
                url = self.url_for(name)
                for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                    for target in (url, f"{url}?role=ADMIN&is_staff=true&is_superuser=true&groups={admin_group.pk}"):
                        self.login_as(actor)
                        self.client.generic(
                            method, target, "&".join(f"{k}={v}" for k, v in ROLE_FORGERY.items()),
                            content_type="application/x-www-form-urlencoded",
                        )
                        if method == "POST":
                            self.login_as(actor)
                            self.client.post(target, body)
        self.assertEqual(role_snapshot(), before)  # no role, group, flag or permission moved; no user created

    def test_registration_ignores_privilege_fields_in_query_string_and_body(self):
        self.client.logout()
        data = {
            "first_name": "Eve", "last_name": "M", "username": "eve", "email": "eve@example.com",
            "student_id": "CYB/eve", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD, **ROLE_FORGERY,
            "groups": [Group.objects.get(name=ADMIN).pk],
        }
        response = self.client.post(reverse("accounts:register") + "?role=ADMIN&is_superuser=true", data)
        self.assertRedirects(response, reverse("accounts:login"))
        eve = User.objects.get(username="eve")
        self.assertEqual(list(eve.groups.values_list("name", flat=True)), [STUDENT])
        self.assertFalse(eve.is_staff or eve.is_superuser)
        self.assertEqual(eve.user_permissions.count(), 0)

    def test_exactly_three_application_roles_exist(self):
        self.assertEqual(ALL_ROLES, ("STUDENT", "LECTURER", "ADMIN"))
        # Created by migration 0004; a typo there would show up as an extra or missing group.
        self.assertEqual(set(Group.objects.values_list("name", flat=True)), set(ALL_ROLES))


# --------------------------------------------------------------------------------
# DELEGATED DJANGO-ADMIN STAFF (finding: password reset / edit of privileged accounts)
# --------------------------------------------------------------------------------
class DelegatedStaffTests(BoundaryTestCase):
    NEW_PASSWORD = "Brand-New-Pass-4711!"

    def setUp(self):
        self.staffer = make_user("staffer", is_staff=True)
        self.staffer.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="accounts",
                content_type__model="user",
                codename__in=["view_user", "change_user", "delete_user", "add_user"],
            )
        )
        self.other_staff = make_user("otherstaff", is_staff=True)
        self.ordinary = make_user("ordinary", STUDENT)

    def password_change(self, target):
        return self.client.post(
            reverse("admin:auth_user_password_change", args=[target.pk]),
            {"password1": self.NEW_PASSWORD, "password2": self.NEW_PASSWORD, "usable_password": "true"},
        )

    def change_payload(self, target, **overrides):
        joined = timezone.localtime(target.date_joined)
        data = {
            "username": target.username, "email": target.email, "first_name": "Changed",
            "last_name": target.last_name, "is_active": "on",
            "date_joined_0": joined.strftime("%Y-%m-%d"), "date_joined_1": joined.strftime("%H:%M:%S"),
        }
        data.update(overrides)
        return data

    def test_control_superuser_can_reset_a_password(self):
        # Proves the attack below is a real operation that succeeds when allowed.
        victim = make_user("ctrl_admin", ADMIN)
        self.client.force_login(self.superuser)
        self.assertEqual(self.password_change(victim).status_code, 302)
        self.assertTrue(User.objects.get(pk=victim.pk).check_password(self.NEW_PASSWORD))

    def test_staff_cannot_reset_passwords_of_privileged_accounts(self):
        self.client.force_login(self.staffer)
        for target in (self.superuser, self.app_admin, self.other_staff):
            with self.subTest(target=target.username):
                self.assertEqual(self.password_change(target).status_code, 403)
                self.assertTrue(User.objects.get(pk=target.pk).check_password(PASSWORD))

    def test_staff_cannot_edit_or_delete_privileged_accounts(self):
        self.client.force_login(self.staffer)
        for target in (self.superuser, self.app_admin, self.other_staff):
            with self.subTest(target=target.username):
                change = self.client.post(
                    reverse("admin:accounts_user_change", args=[target.pk]), self.change_payload(target)
                )
                self.assertEqual(change.status_code, 403)
                self.assertNotEqual(User.objects.get(pk=target.pk).first_name, "Changed")
                delete = self.client.post(reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"})
                self.assertEqual(delete.status_code, 403)
                self.assertTrue(User.objects.filter(pk=target.pk).exists())

    def test_staff_can_still_manage_ordinary_users(self):
        self.client.force_login(self.staffer)
        response = self.client.post(
            reverse("admin:accounts_user_change", args=[self.ordinary.pk]), self.change_payload(self.ordinary)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.get(pk=self.ordinary.pk).first_name, "Changed")
        self.assertEqual(self.password_change(self.ordinary).status_code, 302)

    def test_staff_can_edit_own_account_but_cannot_escalate(self):
        self.client.force_login(self.staffer)
        forged = self.change_payload(self.staffer, is_superuser="on", groups=[Group.objects.get(name=ADMIN).pk])
        response = self.client.post(reverse("admin:accounts_user_change", args=[self.staffer.pk]), forged)
        self.assertEqual(response.status_code, 302)
        me = User.objects.get(pk=self.staffer.pk)
        self.assertEqual(me.first_name, "Changed")
        self.assertFalse(me.is_superuser)
        self.assertEqual(me.groups.count(), 0)
        # ...and cannot delete themselves through the admin either.
        response = self.client.post(reverse("admin:accounts_user_delete", args=[self.staffer.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)


# --------------------------------------------------------------------------------
# CSRF, SESSIONS AND LOGOUT
# --------------------------------------------------------------------------------
class CsrfAndSessionTests(BoundaryTestCase):
    def registration_data(self, token):
        return {
            "first_name": "Zed", "last_name": "Z", "username": "zed", "email": "zed@example.com",
            "student_id": "CYB/zed", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD, "csrfmiddlewaretoken": token,
        }

    def test_csrf_middleware_is_on_and_nothing_is_exempt(self):
        self.assertIn("django.middleware.csrf.CsrfViewMiddleware", settings.MIDDLEWARE)
        for path in Path(settings.BASE_DIR).glob("*/**/*.py"):
            if ".venv" in path.parts or path.name in {"test_boundary.py"}:
                continue
            self.assertNotIn("csrf_exempt", path.read_text(), msg=str(path))

    def test_every_post_form_template_carries_a_csrf_token(self):
        checked = 0
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            text = path.read_text()
            for form in re.findall(r"<form\b[^>]*>.*?</form>", text, flags=re.S):
                if re.search(r'method="post"', form, flags=re.I):
                    checked += 1
                    self.assertIn("{% csrf_token %}", form, msg=str(path))
        self.assertGreaterEqual(checked, 3)  # logout (base), login, register

    def test_state_changing_requests_without_a_token_are_rejected(self):
        client = Client(enforce_csrf_checks=True)
        for url in (reverse("accounts:register"), reverse("accounts:login"), reverse("accounts:logout")):
            self.assertEqual(client.post(url, {"username": "x"}).status_code, 403, url)
        client.force_login(self.alice)
        before = role_snapshot()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for url in (self.student_dashboard(), reverse("accounts:logout"), self.profile_url(self.alice)):
                self.assertEqual(client.generic(method, url, "role=ADMIN").status_code, 403, (method, url))
        self.assertEqual(client.get(self.student_dashboard()).status_code, 200)  # logout was refused
        self.assertEqual(role_snapshot(), before)

    def test_real_browser_flow_works_with_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("accounts:register"))
        response = client.post(
            reverse("accounts:register"), self.registration_data(client.cookies["csrftoken"].value)
        )
        self.assertRedirects(response, reverse("accounts:login"))
        client.get(reverse("accounts:login"))
        response = client.post(
            reverse("accounts:login"),
            {"username": "zed", "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
        )
        self.assertRedirects(response, self.student_dashboard())
        client.get(self.student_dashboard())  # the page's logout form supplies the token
        response = client.post(
            reverse("accounts:logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        )
        self.assertRedirects(response, reverse("accounts:login"))

    def test_session_is_dead_after_logout_even_if_the_cookie_is_replayed(self):
        self.client.post(reverse("accounts:login"), {"username": "alice", "password": PASSWORD})
        stolen = self.client.cookies["sessionid"].value
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)
        self.client.post(reverse("accounts:logout"))
        replay = Client()
        replay.cookies["sessionid"] = stolen
        response = replay.get(self.student_dashboard())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_login_rotates_the_session_key(self):
        session = self.client.session
        session["probe"] = 1
        session.save()
        before = session.session_key
        self.client.post(reverse("accounts:login"), {"username": "alice", "password": PASSWORD})
        self.assertNotEqual(self.client.session.session_key, before)


# --------------------------------------------------------------------------------
# OUTPUT ESCAPING (the only place one user's input is shown to another user)
# --------------------------------------------------------------------------------
class OutputEscapingTests(BoundaryTestCase):
    def test_user_supplied_names_are_escaped_when_an_admin_views_the_profile(self):
        evil = make_student("evil", self.cyb, self.l200)
        evil.first_name = "<script>alert(1)</script>"
        evil.save()
        response = self.get_as(self.app_admin, self.profile_url(evil))
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
