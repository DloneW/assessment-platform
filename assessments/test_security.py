"""
Day 4: security attack simulation.

Every test here is an attack made through a real HTTP request (the Django test client,
the same code path a browser or curl takes). Where a vulnerability was found, the test is
marked FINDING-n and documents the exploit; it failed before the fix and passes after it.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import Enrollment, StudentProfile
from assessments import services
from assessments.models import Attempt, Choice, Question, StudentAnswer, Test
from assessments.tests import AssessmentTestCase, build_test, question_data, right, test_data, wrong
from authorization.roles import STUDENT
from authorization.test_boundary import masked
from authorization.tests import PASSWORD, make_user

User = get_user_model()
HUGE = 10**30  # larger than any 64-bit database integer


def staff_with(username, *codenames):
    """A Django-admin staff account (NOT an application ADMIN) holding exactly these model permissions."""
    user = make_user(username, is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


# --------------------------------------------------------------------------------
# AUTHORIZATION: every student/lecturer attack from the brief, over real requests
# --------------------------------------------------------------------------------
class AuthorizationSecurityTests(AssessmentTestCase):
    def test_student_cannot_reach_staff_pages_or_other_students(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(self.lecturer_dashboard()).status_code, 403)
        self.assertEqual(self.client.get(self.admin_dashboard()).status_code, 403)
        self.assertEqual(self.client.get(self.profile_url(self.bob)).status_code, 404)  # another student's profile
        self.assertEqual(self.client.get(self.course_url(self.cyb205)).status_code, 404)  # course she is not in
        self.assertEqual(self.client.get(self.course_url(self.csc201)).status_code, 404)  # other department
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)  # Django admin: login wall

    def test_student_cannot_manipulate_enrollment_department_or_level_through_any_route(self):
        bob_profile = self.bob.student_profile
        before = (
            list(Enrollment.objects.values_list("student_id", "course_id")),
            list(StudentProfile.objects.values_list("pk", "department_id", "level_id", "student_id")),
        )
        self.login(self.alice)
        forged = {"department": self.csc.pk, "level": self.l300.pk, "student_id": "X/1", "courses": self.cyb205.pk, "course": self.cyb205.pk}
        for url in (
            self.student_dashboard(), self.profile_url(self.alice), self.course_url(self.cyb201),
            reverse("assessments:attempt_start", args=[self.test201.pk]),
            reverse("accounts:register"),
        ):
            for method in ("get", "post", "put", "patch", "delete"):
                getattr(self.client, method)(url, forged)
        after = (
            list(Enrollment.objects.values_list("student_id", "course_id")),
            list(StudentProfile.objects.values_list("pk", "department_id", "level_id", "student_id")),
        )
        self.assertEqual(before, after)
        self.assertEqual(bob_profile.student_id, StudentProfile.objects.get(pk=bob_profile.pk).student_id)

    def test_lecturer_cannot_reach_student_data_or_other_courses(self):
        self.login(self.lect1)
        self.assertEqual(self.client.get(self.profile_url(self.alice)).status_code, 404)
        self.assertEqual(self.client.get(self.course_url(self.cyb205)).status_code, 404)  # lect2's course
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 403)
        self.assertEqual(self.client.get(self.admin_dashboard()).status_code, 403)

    def test_session_is_dead_after_logout_and_old_cookie_cannot_be_replayed(self):
        self.client.post(reverse("accounts:login"), {"username": "alice", "password": PASSWORD})
        stolen = self.client.cookies["sessionid"].value
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)
        self.client.post(reverse("accounts:logout"))
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 302)
        replay = Client()
        replay.cookies["sessionid"] = stolen
        response = replay.get(self.student_dashboard())
        self.assertEqual(response.status_code, 302)  # server-side session was destroyed, not just the cookie

    def test_session_id_changes_on_login(self):
        anonymous = Client()
        anonymous.get(reverse("accounts:login"))
        before = anonymous.cookies.get("sessionid")
        anonymous.post(reverse("accounts:login"), {"username": "alice", "password": PASSWORD})
        self.assertNotEqual(getattr(before, "value", None), anonymous.cookies["sessionid"].value)

    def test_deactivated_user_loses_access_immediately(self):
        self.login(self.alice)
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 200)
        User.objects.filter(pk=self.alice.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.student_dashboard()).status_code, 302)

    def test_lecturer_unassigned_from_course_loses_test_rights_immediately(self):
        self.login(self.lect1)
        self.assertEqual(self.client.get(reverse("assessments:test_manage", args=[self.test201.pk])).status_code, 200)
        self.cyb201.lecturers.remove(self.lect1)
        self.assertEqual(self.client.get(reverse("assessments:test_manage", args=[self.test201.pk])).status_code, 404)

    def test_student_removed_from_course_cannot_start_but_open_pages_are_gone(self):
        Enrollment.objects.filter(student=self.alice.student_profile, course=self.cyb201).delete()
        self.login(self.alice)
        self.assertEqual(self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk])).status_code, 404)
        self.assertEqual(Attempt.objects.count(), 0)

    # --- registration / mass assignment ---
    def test_registration_cannot_create_privileged_accounts(self):
        forged = {
            "first_name": "Eve", "last_name": "X", "username": "eve", "email": "eve@example.com",
            "student_id": "CYB/eve", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD,
            "role": "ADMIN", "groups": "1", "group": "LECTURER", "is_staff": "on", "is_superuser": "on",
            "is_active": "on", "user_permissions": "1", "date_joined": "2000-01-01",
        }
        self.client.post(reverse("accounts:register"), forged)
        eve = User.objects.get(username="eve")
        self.assertEqual((eve.is_staff, eve.is_superuser), (False, False))
        self.assertEqual(list(eve.groups.values_list("name", flat=True)), [STUDENT])
        self.assertEqual(eve.user_permissions.count(), 0)
        self.assertGreater(eve.date_joined.year, 2020)

    # --- open redirect ---
    def test_login_next_parameter_cannot_redirect_off_site(self):
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"):
            with self.subTest(next=target):
                client = Client()
                response = client.post(
                    reverse("accounts:login") + "?next=" + target, {"username": "alice", "password": PASSWORD, "next": target}
                )
                self.assertEqual(response.status_code, 302)
                self.assertFalse(response.url.startswith(("http", "//", "javascript")), response.url)


# --------------------------------------------------------------------------------
# OBJECT ACCESS: IDOR with every kind of id, GET and POST
# --------------------------------------------------------------------------------
class ObjectAccessSecurityTests(AssessmentTestCase):
    def setUp(self):
        super().setUp()
        self.other_test = build_test(self.cyb205, self.lect2, title="Lecturer B test", publish=False)
        self.other_q = self.other_test.questions.first()

    def test_missing_foreign_and_unauthorized_objects_look_identical(self):
        """An attacker must not be able to tell 'exists but not yours' from 'does not exist'."""
        attempt = self.start(self.alice)
        pairs = [
            # (user, foreign url, missing url)
            (self.bob, reverse("assessments:attempt_detail", args=[self.test201.pk, attempt.pk]),
             reverse("assessments:attempt_detail", args=[self.test201.pk, 99999])),
            (self.lect1, reverse("assessments:test_manage", args=[self.other_test.pk]), reverse("assessments:test_manage", args=[99999])),
            (self.alice, reverse("assessments:test_manage", args=[self.test201.pk]), reverse("assessments:test_manage", args=[99999])),
            (self.carol, reverse("assessments:test_intro", args=[self.test201.pk]), reverse("assessments:test_intro", args=[99999])),
            (self.alice, self.profile_url(self.bob), reverse("accounts:student_profile", args=[99999])),
            (self.alice, self.course_url(self.cyb205), reverse("accounts:course_detail", args=[99999])),
            (self.lect1, reverse("assessments:question_edit", args=[self.other_test.pk, self.other_q.pk]),
             reverse("assessments:question_edit", args=[99999, 1])),
        ]
        for user, foreign, missing in pairs:
            self.login(user)
            with self.subTest(user=user.username, url=foreign):
                self.assertEqual(masked(self.client.get(foreign)), masked(self.client.get(missing)))

    def test_cross_lecturer_attacks_over_post_leave_everything_unchanged(self):
        before = self.db_state()
        self.login(self.lect1)  # lecturer A attacks lecturer B's draft
        t, q, c = self.other_test, self.other_q, self.other_q.choices.first()
        attacks = [
            ("assessments:test_edit", [t.pk], test_data(title="owned")),
            ("assessments:question_add", [t.pk], question_data()),
            ("assessments:question_edit", [t.pk, q.pk], question_data(text="owned")),
            ("assessments:question_delete", [t.pk, q.pk], {}),
            ("assessments:test_publish", [t.pk], {}),
            ("assessments:test_archive", [t.pk], {}),
            # choice ids smuggled into question edit of a test lecturer A DOES manage
            ("assessments:question_edit", [self.test201.pk, q.pk], question_data(text="owned", choice_1=str(c.pk))),
        ]
        for name, args, data in attacks:
            with self.subTest(route=name, args=args):
                self.assertEqual(self.client.post(reverse(name, args=args), data).status_code, 404)
        self.assertEqual(self.db_state(), before)

    def test_replacing_ids_in_every_attempt_url_never_crosses_students(self):
        a = self.start(self.alice)
        b = self.start(self.bob)
        self.login(self.alice)
        data = {f"q_{self.q1.pk}": right(self.q1).pk}
        before = self.db_state()
        for name in ("attempt_detail", "attempt_save", "attempt_submit"):
            url = reverse(f"assessments:{name}", args=[self.test201.pk, b.pk])  # Bob's attempt id
            response = self.client.get(url) if name == "attempt_detail" else self.client.post(url, data)
            self.assertEqual(response.status_code, 404, name)
        self.assertEqual(self.db_state(), before)
        # Alice's own attempt with Bob's test/attempt mixed: wrong test id
        other = build_test(self.cyb201, self.lect1, title="Other")
        self.assertEqual(self.client.get(reverse("assessments:attempt_detail", args=[other.pk, a.pk])).status_code, 404)

    def test_another_students_result_and_answers_are_not_readable(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        self.login(self.bob)
        response = self.client.get(self.attempt_url(a))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Score", status_code=404)

    def test_answer_and_attempt_rows_cannot_be_modified_by_lecturer_or_admin_routes(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        before = self.db_state()
        for actor in (self.lect1, self.lect2, self.app_admin, self.superuser):
            self.login(actor)
            for name in ("attempt_save", "attempt_submit"):
                resp = self.client.post(self.attempt_url(a, name), {f"q_{self.q2.pk}": right(self.q2).pk, "score": 100})
                self.assertEqual(resp.status_code, 404, (actor.username, name))
        self.assertEqual(self.db_state(), before)

    def test_huge_integer_ids_do_not_crash_the_server(self):
        """FINDING-1: ids beyond the database integer range must be a clean 404/400, never a 500."""
        self.login(self.lect1)
        urls = [
            reverse("assessments:test_manage", args=[HUGE]),
            reverse("assessments:test_edit", args=[HUGE]),
            reverse("assessments:question_edit", args=[self.test201.pk, HUGE]),
            reverse("assessments:test_intro", args=[HUGE]),
            reverse("assessments:attempt_detail", args=[HUGE, HUGE]),
            reverse("accounts:course_detail", args=[HUGE]),
            reverse("accounts:student_profile", args=[HUGE]),
            reverse("assessments:test_create", args=[HUGE]),
        ]
        c = Client(raise_request_exception=False)
        c.force_login(self.lect1)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(c.get(url).status_code, 404)
        for name, args in [("test_publish", [HUGE]), ("test_archive", [HUGE]), ("attempt_start", [HUGE])]:
            self.assertEqual(c.post(reverse(f"assessments:{name}", args=args)).status_code, 404, name)

    def test_huge_integer_ids_in_answer_payloads_are_a_400_not_a_500(self):
        """FINDING-1b: same for ids in POST bodies."""
        attempt = self.start(self.alice)
        c = Client(raise_request_exception=False)
        c.force_login(self.alice)
        for data in ({f"q_{HUGE}": "1"}, {f"q_{self.q1.pk}": str(HUGE)}, {"q_1_0": "1"}, {"q_١": "1"}, {f"q_{self.q1.pk}": "1_0"}):
            for name in ("attempt_save", "attempt_submit"):
                with self.subTest(data=data, route=name):
                    self.assertEqual(c.post(self.attempt_url(attempt, name), data).status_code, 400)
        self.assertEqual(Attempt.objects.get().status, "IN_PROGRESS")  # rejected submit did not finalize

    def test_flood_of_form_fields_is_rejected_cleanly(self):
        attempt = self.start(self.alice)
        c = Client(raise_request_exception=False)
        c.force_login(self.alice)
        data = {f"q_{i}": "1" for i in range(1, 1501)}
        self.assertEqual(c.post(self.attempt_url(attempt, "attempt_save"), data).status_code, 400)


# --------------------------------------------------------------------------------
# DJANGO ADMIN: a staff account with limited model permissions must not break the freeze
# --------------------------------------------------------------------------------
class AdminSiteSecurityTests(AssessmentTestCase):
    def admin_post(self, user, url, data):
        self.login(user)
        return self.client.post(url, data)

    def test_staff_cannot_add_a_question_to_a_published_test(self):
        """FINDING-2: the freeze must hold in the Django admin too, or max_score and in-flight scoring change."""
        staff = staff_with("staffq", "add_question", "change_question", "view_question", "add_choice", "change_choice")
        attempt = self.start(self.alice)
        self.admin_post(
            staff, reverse("admin:assessments_question_add"),
            {"test": self.test201.pk, "text": "Sneaky", "order": 9, "marks": 50,
             "choices-TOTAL_FORMS": 2, "choices-INITIAL_FORMS": 0, "choices-MIN_NUM_FORMS": 0, "choices-MAX_NUM_FORMS": 1000,
             "choices-0-text": "a", "choices-0-order": 1, "choices-0-is_correct": "on",
             "choices-1-text": "b", "choices-1-order": 2},
        )
        self.assertFalse(Question.objects.filter(text="Sneaky").exists())
        self.answer(self.alice, attempt, {self.q1: right(self.q1)}, name="attempt_submit")
        attempt.refresh_from_db()
        self.assertEqual(attempt.max_score, 5)

    def test_staff_cannot_edit_a_published_question_or_flip_the_answer_key(self):
        staff = staff_with("staffe", "change_question", "view_question", "change_choice")
        before = self.db_state()
        change = reverse("admin:assessments_question_change", args=[self.q1.pk])
        self.assertEqual(self.client.get(change).status_code, 302)  # not logged in
        self.login(staff)
        self.client.post(change, {"test": self.test201.pk, "text": "Edited", "order": 1, "marks": 99,
                                  "choices-TOTAL_FORMS": 0, "choices-INITIAL_FORMS": 0, "choices-MIN_NUM_FORMS": 0, "choices-MAX_NUM_FORMS": 1000})
        self.assertEqual(self.db_state(), before)

    def test_staff_cannot_bulk_delete_questions_of_a_published_test(self):
        """FINDING-3: the admin 'delete selected' action must not bypass the per-object delete rule."""
        staff = staff_with("staffd", "delete_question", "view_question", "change_question")
        before = self.db_state()
        self.login(staff)
        self.client.post(
            reverse("admin:assessments_question_changelist"),
            {"action": "delete_selected", "_selected_action": [self.q1.pk, self.q2.pk], "post": "yes"},
        )
        self.assertEqual(self.db_state(), before)

    def test_staff_cannot_move_or_retime_a_published_test(self):
        """FINDING-4: course/creator/duration of a published test are frozen in the admin as well."""
        staff = staff_with("stafft", "change_test", "view_test")
        self.login(staff)
        self.client.post(
            reverse("admin:assessments_test_change", args=[self.test201.pk]),
            {"course": self.cyb205.pk, "creator": self.lect2.pk, "title": "Moved", "description": "", "duration_minutes": 1},
        )
        t = Test.objects.get(pk=self.test201.pk)
        self.assertEqual((t.course_id, t.creator_id, t.title, t.duration_minutes), (self.cyb201.pk, self.lect1.pk, "Midterm", 30))

    def test_staff_cannot_forge_status_or_publication_time_in_the_admin(self):
        staff = staff_with("staffs", "change_test", "view_test", "add_test")
        draft = build_test(self.cyb201, self.lect1, publish=False, title="D")
        self.login(staff)
        self.client.post(
            reverse("admin:assessments_test_change", args=[draft.pk]),
            {"course": self.cyb201.pk, "creator": self.lect1.pk, "title": "D", "description": "", "duration_minutes": 30,
             "status": "ARCHIVED", "published_at_0": "2020-01-01", "published_at_1": "00:00:00"},
        )
        t = Test.objects.get(pk=draft.pk)
        self.assertEqual((t.status, t.published_at), ("DRAFT", None))

    def test_staff_cannot_edit_attempts_or_answers_in_the_admin(self):
        staff = staff_with("stafft2", "change_attempt", "view_attempt", "delete_attempt", "change_studentanswer", "view_studentanswer", "delete_studentanswer", "add_attempt", "add_studentanswer")
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        before = self.db_state()
        self.login(staff)
        c = self.client
        c.post(reverse("admin:assessments_attempt_change", args=[a.pk]), {"status": "IN_PROGRESS", "score": 99})
        c.post(reverse("admin:assessments_attempt_delete", args=[a.pk]), {"post": "yes"})
        c.post(reverse("admin:assessments_attempt_changelist"), {"action": "delete_selected", "_selected_action": [a.pk], "post": "yes"})
        sa = StudentAnswer.objects.get()
        c.post(reverse("admin:assessments_studentanswer_changelist"), {"action": "delete_selected", "_selected_action": [sa.pk], "post": "yes"})
        c.post(reverse("admin:assessments_studentanswer_delete", args=[sa.pk]), {"post": "yes"})
        self.assertEqual(self.db_state(), before)


# --------------------------------------------------------------------------------
# STATE TRANSITIONS: attack every edge of both state machines over HTTP
# --------------------------------------------------------------------------------
class StateTransitionSecurityTests(AssessmentTestCase):
    FORGED_STATE = {"status": "ARCHIVED", "state": "ARCHIVED", "published_at": "2020-01-01", "archived": "1", "published": "1"}

    def test_test_state_machine_has_no_illegal_edges(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, title="D")
        published, archived = self.test201, build_test(self.cyb203, self.lect1, title="A")
        services.archive_test(archived)
        self.login(self.lect1)

        def status(t):
            return Test.objects.get(pk=t.pk).status

        # DRAFT -> ARCHIVED is refused, even with forged status data
        self.client.post(reverse("assessments:test_archive", args=[draft.pk]), self.FORGED_STATE)
        self.assertEqual(status(draft), "DRAFT")
        # ARCHIVED -> PUBLISHED / DRAFT are refused
        self.client.post(reverse("assessments:test_publish", args=[archived.pk]))
        self.client.post(reverse("assessments:test_edit", args=[archived.pk]), test_data(status="DRAFT"))
        self.assertEqual(status(archived), "ARCHIVED")
        # PUBLISHED -> DRAFT: no route exists; edit with forged status keeps it published
        self.client.post(reverse("assessments:test_edit", args=[published.pk]), test_data(status="DRAFT", published_at=""))
        self.assertEqual(status(published), "PUBLISHED")
        self.assertIsNotNone(Test.objects.get(pk=published.pk).published_at)
        # DRAFT -> PUBLISHED by a user who may not manage it
        for outsider in (self.lect2, self.alice):
            self.login(outsider)
            self.client.post(reverse("assessments:test_publish", args=[draft.pk]), self.FORGED_STATE)
        self.assertEqual(status(draft), "DRAFT")

    def test_status_cannot_be_chosen_through_any_authoring_form(self):
        self.login(self.lect1)
        self.client.post(reverse("assessments:test_create", args=[self.cyb201.pk]), test_data(status="PUBLISHED", published_at="2020-01-01"))
        t = Test.objects.get(title="Quiz 1")
        self.assertEqual((t.status, t.published_at), ("DRAFT", None))
        self.client.post(reverse("assessments:test_edit", args=[t.pk]), test_data(title="Quiz 2", status="PUBLISHED"))
        t.refresh_from_db()
        self.assertEqual((t.title, t.status), ("Quiz 2", "DRAFT"))
        self.client.post(reverse("assessments:question_add", args=[t.pk]), question_data(status="PUBLISHED", test=self.test201.pk, order=77))
        q = t.questions.get()
        self.assertEqual((q.test_id, q.order), (t.pk, 1))  # test/order are server-assigned

    def test_attempt_state_machine_has_no_illegal_edges(self):
        a = self.start(self.alice)
        forged = {"status": "EXPIRED", "score": 5, "max_score": 5, "submitted_at": "2000-01-01", "started_at": "2000-01-01", "deadline": "2999-01-01"}
        # IN_PROGRESS -> EXPIRED by forged data: refused
        for name in ("attempt_save", "attempt_submit"):
            self.answer(self.alice, a, {}, name=name, **forged)
            a.refresh_from_db()
            if name == "attempt_save":
                self.assertEqual((a.status, a.score, a.submitted_at), ("IN_PROGRESS", None, None))
        self.assertEqual((a.status, a.score), ("SUBMITTED", 0))  # submit is the only edge, and score is server-computed
        # SUBMITTED -> IN_PROGRESS / EXPIRED / re-scored: refused
        snapshot = Attempt.objects.filter(pk=a.pk).values().get()
        for name in ("attempt_save", "attempt_submit"):
            self.answer(self.alice, a, {self.q1: right(self.q1)}, name=name, **forged)
        self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk]), forged)
        self.assertEqual(Attempt.objects.filter(pk=a.pk).values().get(), snapshot)

    def test_expired_attempt_cannot_return_to_in_progress_or_be_submitted(self):
        a = self.start(self.alice)
        now = timezone.now()
        Attempt.objects.filter(pk=a.pk).update(started_at=now - timedelta(hours=1), deadline=now - timedelta(minutes=1))
        self.login(self.alice)
        self.client.get(self.attempt_url(a))  # lazily expires
        snapshot = Attempt.objects.filter(pk=a.pk).values().get()
        self.assertEqual(snapshot["status"], "EXPIRED")
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit", status="SUBMITTED", score=5)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_save", status="IN_PROGRESS")
        self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk]), {"status": "IN_PROGRESS"})
        self.assertEqual(Attempt.objects.filter(pk=a.pk).values().get(), snapshot)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_database_rejects_impossible_attempt_transitions_even_from_trusted_code(self):
        a = self.start(self.alice)
        services.submit_attempt(a)
        a.refresh_from_db()
        for fields in (
            {"status": "IN_PROGRESS"},  # reopening while keeping score/submitted_at
            {"status": "EXPIRED"},  # SUBMITTED -> EXPIRED keeping submitted_at
            {"score": a.max_score + 1},
            {"submitted_at": a.started_at - timedelta(seconds=1)},  # FINDING-5: submitted before it started
            {"submitted_at": a.deadline + timedelta(seconds=1)},  # FINDING-5: SUBMITTED after the deadline
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                Attempt.objects.filter(pk=a.pk).update(**fields)


# --------------------------------------------------------------------------------
# ATTEMPT INTEGRITY
# --------------------------------------------------------------------------------
class AttemptSecurityTests(AssessmentTestCase):
    def test_student_cannot_create_attempts_for_other_students_or_tests(self):
        other_test = build_test(self.cyb201, self.lect1, title="Other")
        draft = build_test(self.cyb201, self.lect1, publish=False, title="Draft")
        self.login(self.alice)
        self.client.post(
            reverse("assessments:attempt_start", args=[self.test201.pk]),
            {"student": self.bob.student_profile.pk, "test": other_test.pk, "test_id": draft.pk},
        )
        a = Attempt.objects.get()
        self.assertEqual((a.student_id, a.test_id), (self.alice.student_profile.pk, self.test201.pk))
        self.assertEqual(self.client.post(reverse("assessments:attempt_start", args=[draft.pk])).status_code, 404)

    def test_cross_test_and_cross_question_answers_are_rejected_over_http(self):
        a = self.start(self.alice)
        other = build_test(self.cyb201, self.lect1, title="Other")
        oq = other.questions.first()
        before = self.db_state()
        self.login(self.alice)
        url = self.attempt_url(a, "attempt_save")
        for data in (
            {f"q_{oq.pk}": right(oq).pk},  # question from another test
            {f"q_{self.q1.pk}": right(oq).pk},  # choice from another test's question
            {f"q_{self.q1.pk}": right(self.q2).pk},  # choice from another question, same test
            {f"q_{self.q1.pk}": right(self.q1).pk, f"q_{oq.pk}": right(oq).pk},  # valid + invalid: all-or-nothing
        ):
            with self.subTest(data=data):
                self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.db_state(), before)

    def test_no_duplicate_answers_whatever_the_request_looks_like(self):
        a = self.start(self.alice)
        self.login(self.alice)
        url = self.attempt_url(a, "attempt_save")
        for _ in range(3):
            self.client.post(url, {f"q_{self.q1.pk}": right(self.q1).pk})
        self.client.post(url, {f"q_{self.q1.pk}": [wrong(self.q1).pk, right(self.q1).pk], f"q_0{self.q1.pk}": wrong(self.q1).pk})
        self.assertEqual(StudentAnswer.objects.filter(attempt=a, question=self.q1).count(), 1)

    def test_repeat_submit_is_a_silent_no_op_and_never_reports_success_again(self):
        a = self.start(self.alice)
        first = self.client.post(self.attempt_url(a, "attempt_submit"), follow=True)
        self.assertIn("Test submitted.", [str(m) for m in first.context["messages"]])
        second = self.client.post(self.attempt_url(a, "attempt_submit"), {f"q_{self.q1.pk}": right(self.q1).pk}, follow=True)
        self.assertNotIn("Test submitted.", [str(m) for m in second.context["messages"]])
        self.assertEqual(StudentAnswer.objects.count(), 0)

    def test_answers_cannot_be_changed_after_submission_or_expiry(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: wrong(self.q1)}, name="attempt_submit")
        before = self.db_state()
        for _ in range(2):
            self.answer(self.alice, a, {self.q1: right(self.q1), self.q2: right(self.q2)})
            self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        self.assertEqual(self.db_state(), before)

    def test_in_flight_attempt_survives_archiving_and_cannot_be_hijacked_meanwhile(self):
        a = self.start(self.alice)
        services.archive_test(self.test201)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        a.refresh_from_db()
        self.assertEqual((a.status, a.score), ("SUBMITTED", 2))  # existing rights end by deadline, not by archiving


# --------------------------------------------------------------------------------
# SERVER-SIDE SCORING
# --------------------------------------------------------------------------------
class ScoringSecurityTests(AssessmentTestCase):
    def test_no_client_value_can_raise_the_score(self):
        a = self.start(self.alice)
        payload = {
            "score": 5, "max_score": 5, "percentage": 100, "correct_answers": 2, "num_correct": 2, "total": 5,
            "is_correct": "true", "correct": "true", "marks": 100, f"marks_{self.q1.pk}": 100, f"is_correct_{self.q1.pk}": "1",
            f"choice_{self.q1.pk}": right(self.q1).pk, "result": "passed",
        }
        self.answer(self.alice, a, {self.q1: wrong(self.q1), self.q2: wrong(self.q2)}, name="attempt_submit", **payload)
        a.refresh_from_db()
        self.assertEqual((a.score, a.max_score), (0, 5))
        self.assertContains(self.client.get(self.attempt_url(a)), "0 / 5")

    def test_score_is_marks_weighted_and_only_counts_correct_choices(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1), self.q2: wrong(self.q2)}, name="attempt_submit")
        a.refresh_from_db()
        self.assertEqual(a.score, 2)  # q1 is worth 2; q2 (3 marks) wrong

    def test_published_questions_and_key_cannot_be_changed_through_the_app_so_results_cannot_drift(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_save")
        before = self.db_state()
        self.login(self.lect1)
        for name, args, data in [
            ("question_edit", [self.test201.pk, self.q1.pk], question_data(correct="1", marks="100")),
            ("question_add", [self.test201.pk], question_data(marks="100")),
            ("question_delete", [self.test201.pk, self.q1.pk], {}),
            ("test_edit", [self.test201.pk], test_data(duration_minutes="1")),
            ("test_publish", [self.test201.pk], {}),
        ]:
            self.client.post(reverse(f"assessments:{name}", args=args), data)
        self.assertEqual(self.db_state(), before)
        self.answer(self.alice, a, {}, name="attempt_submit")
        a.refresh_from_db()
        self.assertEqual((a.score, a.max_score), (2, 5))

    def test_stored_result_does_not_change_if_the_key_is_altered_after_finalization(self):
        """Documented limitation: no snapshot of questions. A FINALIZED attempt keeps its stored score regardless."""
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        a.refresh_from_db()
        Choice.objects.filter(question=self.q1).update(is_correct=False)  # trusted-code/DB tampering, not reachable via HTTP
        a.refresh_from_db()
        self.assertEqual(a.score, 2)
        self.assertContains(self.client.get(self.attempt_url(a)), "2 / 5")


# --------------------------------------------------------------------------------
# SERVER-SIDE TIMING
# --------------------------------------------------------------------------------
class TimingSecurityTests(AssessmentTestCase):
    def expire(self, a):
        now = timezone.now()
        Attempt.objects.filter(pk=a.pk).update(started_at=now - timedelta(minutes=40), deadline=now - timedelta(minutes=1))

    def test_forged_time_fields_never_move_the_deadline(self):
        self.login(self.alice)
        self.client.post(
            reverse("assessments:attempt_start", args=[self.test201.pk]),
            {"started_at": "2099-01-01 00:00", "deadline": "2099-01-01 00:00", "duration_minutes": 9999, "submitted_at": "2000-01-01"},
        )
        a = Attempt.objects.get()
        self.assertEqual(a.deadline - a.started_at, timedelta(minutes=30))
        self.assertLess(abs((a.started_at - timezone.now()).total_seconds()), 60)
        self.answer(self.alice, a, {}, name="attempt_save", deadline="2099-01-01 00:00", started_at="2099-01-01 00:00")
        a.refresh_from_db()
        self.assertEqual(a.deadline - a.started_at, timedelta(minutes=30))

    def test_late_requests_cannot_be_made_on_time_by_claiming_an_early_timestamp(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)})
        self.expire(a)
        self.answer(self.alice, a, {self.q2: right(self.q2)}, name="attempt_submit", submitted_at="2000-01-01 00:00", now="2000-01-01", client_time="2000-01-01T00:00:00Z")
        a.refresh_from_db()
        self.assertEqual((a.status, a.score, a.submitted_at), ("EXPIRED", 2, None))
        self.assertEqual(StudentAnswer.objects.count(), 1)

    def test_expired_attempt_cannot_be_resumed_saved_or_resubmitted(self):
        a = self.start(self.alice)
        self.expire(a)
        before = None
        self.answer(self.alice, a, {self.q1: right(self.q1)})
        before = self.db_state()
        self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk]))
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        self.answer(self.alice, a, {self.q1: right(self.q1)}, ajax="1")
        self.assertEqual(self.db_state(), before)
        self.assertContains(self.client.get(self.attempt_url(a)), "Expired")

    def test_deadline_check_survives_duration_edits_and_window_changes(self):
        a = self.start(self.alice)
        Test.objects.filter(pk=self.test201.pk).update(duration_minutes=600, closes_at=timezone.now() + timedelta(days=9))
        a.refresh_from_db()
        self.assertEqual(a.deadline - a.started_at, timedelta(minutes=30))


# --------------------------------------------------------------------------------
# INPUT SECURITY: XSS, injection, headers
# --------------------------------------------------------------------------------
XSS = "<script>alert(1)</script>"
XSS_ATTR = '"><img src=x onerror=alert(1)>'


class InputSecurityTests(AssessmentTestCase):
    def assertEscaped(self, response, payload=XSS):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(payload.encode(), response.content)

    def test_registration_fields_are_escaped_everywhere_they_are_displayed(self):
        c = Client()
        c.post(reverse("accounts:register"), {
            "first_name": XSS, "last_name": XSS_ATTR, "username": "xss1", "email": "xss1@example.com",
            "student_id": "CYB/<b>x</b>", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD,
        })
        user = User.objects.get(username="xss1")
        Enrollment.objects.create(student=user.student_profile, course=self.cyb201)
        c.force_login(user)
        for url in (self.student_dashboard(), self.profile_url(user)):
            response = c.get(url)
            self.assertEscaped(response)
            self.assertEscaped(response, XSS_ATTR)
            self.assertNotIn(b"<b>x</b>", response.content)

    def test_course_and_department_fields_are_escaped(self):
        self.cyb201.title = XSS
        self.cyb201.save()
        self.cyb.name = XSS_ATTR
        self.cyb.save()
        for user in (self.alice, self.lect1):
            self.login(user)
            for url in (self.course_url(self.cyb201), self.student_dashboard() if user is self.alice else self.lecturer_dashboard()):
                response = self.client.get(url)
                self.assertEscaped(response)
                self.assertEscaped(response, XSS_ATTR)

    def test_test_question_and_choice_text_are_escaped_on_every_page(self):
        t = build_test(self.cyb201, self.lect1, publish=False, marks=(), title=XSS, description=XSS_ATTR)
        self.login(self.lect1)
        self.client.post(reverse("assessments:question_add", args=[t.pk]),
                         question_data(text=XSS, choice_1=XSS_ATTR, choice_2=XSS, choice_3="ok", correct="3"))
        for url in (reverse("assessments:test_manage", args=[t.pk]), self.lecturer_dashboard(), self.course_url(self.cyb201),
                    reverse("assessments:test_edit", args=[t.pk]), reverse("assessments:question_edit", args=[t.pk, t.questions.get().pk])):
            response = self.client.get(url)
            self.assertEscaped(response)
            self.assertEscaped(response, XSS_ATTR)
        services.publish_test(t)
        a = self.start(self.alice, t)
        for url in (self.attempt_url(a), reverse("assessments:test_intro", args=[t.pk]), self.student_dashboard()):
            response = self.client.get(url)
            self.assertEscaped(response)
            self.assertEscaped(response, XSS_ATTR)
        self.answer(self.alice, a, {}, name="attempt_submit")
        self.assertEscaped(self.client.get(self.attempt_url(a)))

    def test_error_messages_do_not_render_user_input_unescaped(self):
        self.login(self.lect1)
        response = self.client.post(reverse("assessments:test_create", args=[self.cyb201.pk]), test_data(title=XSS, duration_minutes=XSS))
        self.assertEscaped(response)

    def test_sql_injection_strings_are_inert(self):
        evil = "x'); DROP TABLE assessments_test;--"
        self.login(self.lect1)
        self.client.post(reverse("assessments:test_create", args=[self.cyb201.pk]), test_data(title=evil))
        self.assertTrue(Test.objects.filter(title=evil).exists())
        a = self.start(self.alice)
        self.answer(self.alice, a, {}, **{f"q_{self.q1.pk}": f"{right(self.q1).pk} OR 1=1"})
        self.assertEqual(self.client.post(self.attempt_url(a, "attempt_save"), {f"q_{self.q1.pk}": "1 OR 1=1"}).status_code, 400)
        self.assertGreater(Test.objects.count(), 0)
        self.assertEqual(Client().post(reverse("accounts:login"), {"username": "' OR '1'='1", "password": "x"}).status_code, 200)

    def test_source_has_no_unsafe_html_or_raw_sql(self):
        from pathlib import Path
        from django.conf import settings

        banned_templates = ("|safe", "autoescape off", "mark_safe", "{% autoescape")
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            text = path.read_text()
            for token in banned_templates:
                self.assertNotIn(token, text, f"{path}: {token}")
        for app in ("accounts", "assessments", "authorization"):
            for path in Path(settings.BASE_DIR, app).rglob("*.py"):
                if path.name.startswith("test") or "migrations" in path.parts:
                    continue
                text = path.read_text()
                for token in ("mark_safe", ".raw(", ".extra(", "cursor()", "RawSQL", "format_html("):
                    self.assertNotIn(token, text, f"{path}: {token}")

    def test_every_post_route_rejects_requests_without_a_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        a = self.start(self.alice)
        draft = build_test(self.cyb201, self.lect1, publish=False, title="D")
        q = draft.questions.first()
        routes = [
            (self.alice, reverse("accounts:logout")),
            (self.alice, reverse("assessments:attempt_start", args=[self.test201.pk])),
            (self.alice, self.attempt_url(a, "attempt_save")),
            (self.alice, self.attempt_url(a, "attempt_submit")),
            (self.lect1, reverse("assessments:test_create", args=[self.cyb201.pk])),
            (self.lect1, reverse("assessments:test_edit", args=[draft.pk])),
            (self.lect1, reverse("assessments:question_add", args=[draft.pk])),
            (self.lect1, reverse("assessments:question_edit", args=[draft.pk, q.pk])),
            (self.lect1, reverse("assessments:question_delete", args=[draft.pk, q.pk])),
            (self.lect1, reverse("assessments:test_publish", args=[draft.pk])),
            (self.lect1, reverse("assessments:test_archive", args=[self.test201.pk])),
        ]
        before = self.db_state()
        for user, url in routes:
            strict.force_login(user)
            with self.subTest(url=url):
                self.assertEqual(strict.post(url, {}).status_code, 403)
        for anon_url in (reverse("accounts:login"), reverse("accounts:register")):
            self.assertEqual(Client(enforce_csrf_checks=True).post(anon_url, {}).status_code, 403)
        self.assertEqual(self.db_state(), before)

    def test_authenticated_pages_are_not_cacheable(self):
        """FINDING-6: results and the answer key must not be recoverable from the browser cache after logout."""
        a = self.start(self.alice)
        for url in (self.student_dashboard(), self.attempt_url(a), self.profile_url(self.alice)):
            cc = self.client.get(url)["Cache-Control"]
            self.assertIn("no-store", cc, url)
        self.login(self.lect1)
        self.assertIn("no-store", self.client.get(reverse("assessments:test_manage", args=[self.test201.pk]))["Cache-Control"])

    def test_clickjacking_and_cookie_flags(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.client.post(reverse("accounts:login"), {"username": "alice", "password": PASSWORD})
        session = self.client.cookies["sessionid"]
        self.assertTrue(session["httponly"])
        self.assertEqual(session["samesite"], "Lax")

    def test_error_pages_do_not_leak_details(self):
        self.login(self.alice)
        for url in (self.profile_url(self.bob), reverse("assessments:test_manage", args=[self.test201.pk])):
            body = self.client.get(url).content.decode().lower()
            for leak in ("traceback", "permission", "forbidden", "exists", "not authorized", "sqlite", "django.db"):
                self.assertNotIn(leak, body)
        body = self.client.get(self.lecturer_dashboard()).content.decode().lower()
        self.assertNotIn("traceback", body)


# --------------------------------------------------------------------------------
# DATABASE INTEGRITY
# --------------------------------------------------------------------------------
class DatabaseIntegrityTests(AssessmentTestCase):
    def rejects(self, fn):
        with self.assertRaises(IntegrityError), transaction.atomic():
            fn()

    def test_usernames_are_unique_case_insensitively(self):
        """FINDING-7: 'Alice' must not be registrable next to 'alice' (impersonation / login confusion)."""
        form = {
            "first_name": "A", "last_name": "B", "username": "ALICE", "email": "fresh@example.com",
            "student_id": "CYB/fresh", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD,
        }
        response = Client().post(reverse("accounts:register"), form)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username__iexact="alice").count(), 1)
        self.rejects(lambda: User.objects.create_user(username="Alice", email="other@example.com", password=PASSWORD))

    def test_student_ids_and_emails_are_unique_case_insensitively(self):
        profile = self.alice.student_profile
        self.rejects(lambda: StudentProfile.objects.create(user=make_user("zed"), student_id=profile.student_id.lower(), department=self.cyb, level=self.l200))
        self.rejects(lambda: User.objects.create_user(username="zed2", email=self.alice.email.upper(), password=PASSWORD))

    def test_enrollment_is_unique(self):
        self.rejects(lambda: Enrollment.objects.create(student=self.alice.student_profile, course=self.cyb201))

    def test_blank_and_zero_values_are_rejected_by_the_database(self):
        """FINDING-8: invariants the forms enforce must also hold for rows created by any other code path."""
        draft = build_test(self.cyb201, self.lect1, publish=False, marks=(1,), title="D")
        q = draft.questions.get()
        self.rejects(lambda: Test.objects.create(course=self.cyb201, creator=self.lect1, title=""))
        self.rejects(lambda: Question.objects.create(test=draft, text="", order=5))
        self.rejects(lambda: Question.objects.create(test=draft, text="x", order=0))
        self.rejects(lambda: Choice.objects.create(question=q, text="", order=9))
        self.rejects(lambda: Choice.objects.create(question=q, text="x", order=0))

    def test_orphan_and_cross_object_rows_are_rejected(self):
        a = self.start(self.alice)
        from django.db import connection

        def orphan_attempt():
            Attempt.objects.create(student_id=999999, test=self.test201, deadline=timezone.now() + timedelta(minutes=1))
            connection.check_constraints()  # SQLite checks foreign keys at commit; force the check now

        def orphan_answer():
            StudentAnswer.objects.bulk_create([StudentAnswer(attempt=a, question_id=999999, choice_id=999999)])
            connection.check_constraints()

        self.rejects(orphan_attempt)
        self.rejects(orphan_answer)

    def test_protected_rows_cannot_be_deleted_out_from_under_attempts(self):
        from django.db.models import ProtectedError

        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q1: right(self.q1)}, name="attempt_submit")
        for obj in (self.test201, self.q1, right(self.q1), self.cyb201, self.alice.student_profile):
            with self.subTest(obj=obj), self.assertRaises(ProtectedError):
                obj.delete()


class UsernameNormalizationTests(AssessmentTestCase):
    def test_compatibility_characters_are_normalized_so_the_user_can_log_in(self):
        """A username stored un-normalized could never be typed again: login normalizes to NFKC."""
        form = {
            "first_name": "F", "last_name": "L", "username": "ｚoe", "email": "zoe@example.com",  # fullwidth z
            "student_id": "CYB/zoe", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD,
        }
        Client().post(reverse("accounts:register"), form)
        self.assertTrue(User.objects.filter(username="zoe").exists())
        c = Client()
        self.assertEqual(c.post(reverse("accounts:login"), {"username": "ｚoe", "password": PASSWORD}).status_code, 302)
        self.assertIn("sessionid", c.cookies)

    def test_fullwidth_lookalike_of_an_existing_username_is_a_duplicate(self):
        form = {
            "first_name": "F", "last_name": "L", "username": "ａlice", "email": "x@example.com",
            "student_id": "CYB/x", "department": self.cyb.pk, "level": self.l200.pk,
            "password1": PASSWORD, "password2": PASSWORD,
        }
        self.assertEqual(Client().post(reverse("accounts:register"), form).status_code, 200)
        self.assertFalse(User.objects.filter(email="x@example.com").exists())
