"""
Day 3 tests: the assessment engine.

Layout follows the layers: model/DB integrity, authorization decisions, lecturer workflow,
student access, attempts and scoring, server-side timing, then attack cases. The attack
tests play the malicious client: they change ids, forge fields and replay requests, then
assert both the response AND that nothing in the database changed.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import Course
from authorization import services as authz
from authorization.roles import STUDENT
from authorization.tests import AuthzTestCase, make_student, make_user
from assessments import services
from assessments.models import Attempt, Choice, Question, StudentAnswer, Test


# --------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------
def build_test(course, creator, marks=(1, 1), publish=True, title="Midterm", **extra):
    """A test with one question per entry in `marks`; each has choices right(1)/wrong1/wrong2."""
    test = services.create_test(
        course=course,
        creator=creator,
        cleaned={"title": title, "duration_minutes": extra.pop("duration_minutes", 30), **extra},
    )
    for number, mark in enumerate(marks, start=1):
        services.create_question(
            test, {"text": f"Question {number}", "marks": mark, "choices": ["right", "wrong1", "wrong2"], "correct": 1}
        )
    if publish:
        test = services.publish_test(test)
    return test


def right(question):
    return question.choices.get(is_correct=True)


def wrong(question):
    return question.choices.filter(is_correct=False).first()


def question_data(**overrides):
    data = {"text": "What is 2+2?", "marks": "2", "choice_1": "3", "choice_2": "4", "choice_3": "5", "correct": "2"}
    data.update(overrides)
    return data


def test_data(**overrides):
    data = {"title": "Quiz 1", "description": "Be quick", "duration_minutes": "20"}
    data.update(overrides)
    return data


class AssessmentTestCase(AuthzTestCase):
    """Alice/bob are eligible for CYB201; carol (wrong dept), dave (wrong level) are not;
    lect1 manages CYB201/203; lect2 only CYB205."""

    def setUp(self):
        self.test201 = build_test(self.cyb201, self.lect1, marks=(2, 3))
        self.q1, self.q2 = self.test201.questions.all()

    def login(self, user):
        self.client.logout()
        self.client.force_login(user)

    def start(self, user, test=None):
        self.login(user)
        test = test or self.test201
        self.client.post(reverse("assessments:attempt_start", args=[test.pk]))
        return Attempt.objects.get(student=user.student_profile, test=test)

    def attempt_url(self, attempt, name="attempt_detail"):
        return reverse(f"assessments:{name}", args=[attempt.test_id, attempt.pk])

    def answer(self, user, attempt, selections, name="attempt_save", **extra):
        self.login(user)
        data = {f"q_{q.pk}": c.pk for q, c in selections.items()}
        data.update(extra)
        return self.client.post(self.attempt_url(attempt, name), data)

    def db_state(self):
        """Everything an attacker might try to change."""
        return (
            list(Test.objects.values_list("pk", "status", "title", "course_id", "creator_id", "published_at")),
            list(Question.objects.values_list("pk", "text", "marks", "order")),
            list(Choice.objects.values_list("pk", "text", "is_correct")),
            list(
                Attempt.objects.values_list(
                    "pk", "student_id", "test_id", "status", "score", "max_score", "started_at", "deadline", "submitted_at"
                )
            ),
            list(StudentAnswer.objects.values_list("pk", "attempt_id", "question_id", "choice_id")),
        )


# --------------------------------------------------------------------------------
# DATABASE INTEGRITY
# --------------------------------------------------------------------------------
class ModelIntegrityTests(AssessmentTestCase):
    def assertDbRejects(self, fn):
        with self.assertRaises(IntegrityError), transaction.atomic():
            fn()

    def test_duplicate_question_order_rejected(self):
        self.assertDbRejects(lambda: Question.objects.create(test=self.test201, text="x", order=1))

    def test_zero_marks_rejected_by_database(self):
        self.assertDbRejects(lambda: Question.objects.create(test=self.test201, text="x", order=9, marks=0))

    def test_two_correct_choices_for_one_question_rejected(self):
        self.assertDbRejects(lambda: Choice.objects.create(question=self.q1, text="also", order=9, is_correct=True))

    def test_duplicate_choice_order_rejected(self):
        self.assertDbRejects(lambda: Choice.objects.create(question=self.q1, text="dup", order=1))

    def test_duration_bounds_enforced_by_database(self):
        for bad in (0, 601):
            with self.subTest(duration=bad):
                self.assertDbRejects(
                    lambda bad=bad: Test.objects.create(course=self.cyb201, creator=self.lect1, title="t", duration_minutes=bad)
                )

    def test_window_must_close_after_it_opens(self):
        now = timezone.now()
        self.assertDbRejects(
            lambda: Test.objects.create(
                course=self.cyb201, creator=self.lect1, title="t", opens_at=now, closes_at=now - timedelta(hours=1)
            )
        )

    def test_publication_state_must_be_consistent(self):
        self.assertDbRejects(
            lambda: Test.objects.create(
                course=self.cyb201, creator=self.lect1, title="t", status="PUBLISHED", published_at=None
            )
        )
        self.assertDbRejects(
            lambda: Test.objects.create(
                course=self.cyb201, creator=self.lect1, title="t", status="DRAFT", published_at=timezone.now()
            )
        )

    def test_one_attempt_per_student_per_test(self):
        attempt = self.start(self.alice)
        self.assertDbRejects(
            lambda: Attempt.objects.create(
                student=attempt.student, test=self.test201, deadline=timezone.now() + timedelta(minutes=5)
            )
        )

    def test_attempt_state_and_score_must_agree(self):
        now = timezone.now()
        student = self.alice.student_profile
        other = build_test(self.cyb203, self.lect1)
        # in progress but already scored
        self.assertDbRejects(
            lambda: Attempt.objects.create(student=student, test=other, deadline=now + timedelta(minutes=5), score=1, max_score=2)
        )
        # submitted without a score
        self.assertDbRejects(
            lambda: Attempt.objects.create(
                student=student, test=other, deadline=now + timedelta(minutes=5), status="SUBMITTED", submitted_at=now
            )
        )
        # score above max
        self.assertDbRejects(
            lambda: Attempt.objects.create(
                student=student, test=other, deadline=now + timedelta(minutes=5), status="SUBMITTED",
                submitted_at=now, score=5, max_score=2,
            )
        )
        # deadline before start
        self.assertDbRejects(
            lambda: Attempt.objects.create(student=student, test=other, started_at=now, deadline=now - timedelta(minutes=1))
        )

    def test_one_answer_per_question_per_attempt(self):
        attempt = self.start(self.alice)
        StudentAnswer.objects.create(attempt=attempt, question=self.q1, choice=right(self.q1))
        self.assertDbRejects(lambda: StudentAnswer.objects.create(attempt=attempt, question=self.q1, choice=wrong(self.q1)))

    def test_answer_rejects_question_from_another_test(self):
        attempt = self.start(self.alice)
        other = build_test(self.cyb201, self.lect1, title="Other")
        foreign = other.questions.first()
        with self.assertRaises(ValidationError):
            StudentAnswer.objects.create(attempt=attempt, question=foreign, choice=right(foreign))

    def test_answer_rejects_choice_from_another_question(self):
        attempt = self.start(self.alice)
        with self.assertRaises(ValidationError):
            StudentAnswer.objects.create(attempt=attempt, question=self.q1, choice=right(self.q2))

    def test_answer_rejected_once_attempt_is_closed_even_with_stale_object(self):
        attempt = self.start(self.alice)
        stale = Attempt.objects.get(pk=attempt.pk)  # still says IN_PROGRESS in memory
        services.submit_attempt(attempt)
        with self.assertRaises(ValidationError):
            StudentAnswer.objects.create(attempt=stale, question=self.q1, choice=right(self.q1))


# --------------------------------------------------------------------------------
# AUTHORIZATION DECISIONS
# --------------------------------------------------------------------------------
class AssessmentAuthorizationTests(AssessmentTestCase):
    def test_management_rights_follow_course_assignment(self):
        t = self.test201
        self.assertTrue(authz.can_manage_test(self.lect1, t))
        self.assertTrue(authz.can_manage_test(self.app_admin, t))
        for outsider in (self.lect2, self.alice, self.bob, self.superuser):
            with self.subTest(user=outsider.username):
                self.assertFalse(authz.can_manage_test(outsider, t))
                self.assertFalse(authz.can_edit_test(outsider, t))
                self.assertFalse(authz.can_publish_test(outsider, t))

    def test_create_requires_management_and_an_open_course(self):
        self.assertTrue(authz.can_create_test(self.lect1, self.cyb201))
        self.assertFalse(authz.can_create_test(self.lect2, self.cyb201))
        self.assertFalse(authz.can_create_test(self.alice, self.cyb201))
        self.assertFalse(authz.can_create_test(self.lect1, self.inactive))  # managed but inactive

    def test_published_test_cannot_be_edited_or_republished(self):
        self.assertFalse(authz.can_edit_test(self.lect1, self.test201))
        self.assertFalse(authz.can_publish_test(self.lect1, self.test201))
        self.assertTrue(authz.can_archive_test(self.lect1, self.test201))

    def test_student_access_needs_published_test_and_course_access(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, title="Draft")
        self.assertTrue(authz.can_access_test(self.alice, self.test201))
        self.assertTrue(authz.can_access_test(self.bob, self.test201))
        self.assertFalse(authz.can_access_test(self.alice, draft))  # draft
        self.assertFalse(authz.can_access_test(self.carol, self.test201))  # wrong department
        self.assertFalse(authz.can_access_test(self.dave, self.test201))  # wrong level
        # bob is enrolled in CYB205 but not CYB203
        t203 = build_test(self.cyb203, self.lect1, title="203")
        self.assertTrue(authz.can_access_test(self.alice, t203))
        self.assertFalse(authz.can_access_test(self.bob, t203))  # not enrolled

    def test_inactive_course_hides_its_tests_from_students(self):
        t = build_test(self.inactive, self.lect1, title="old")  # created via service, bypassing can_create
        self.assertFalse(authz.can_access_test(self.alice, t))
        self.assertFalse(authz.can_publish_test(self.lect1, build_test(self.inactive, self.lect1, publish=False)))

    def test_start_requires_open_window(self):
        now = timezone.now()
        self.assertTrue(authz.can_start_attempt(self.alice, self.test201, now))
        Test.objects.filter(pk=self.test201.pk).update(opens_at=now + timedelta(hours=1))
        self.assertFalse(authz.can_start_attempt(self.alice, Test.objects.get(pk=self.test201.pk), now))
        Test.objects.filter(pk=self.test201.pk).update(opens_at=None, closes_at=now - timedelta(seconds=1))
        self.assertFalse(authz.can_start_attempt(self.alice, Test.objects.get(pk=self.test201.pk), now))

    def test_lecturer_and_admin_cannot_take_tests(self):
        for user in (self.lect1, self.app_admin):
            self.assertFalse(authz.can_start_attempt(user, self.test201))
            self.assertFalse(authz.can_take_test(user, self.test201))

    def test_attempt_decisions_belong_to_the_owner_only(self):
        attempt = self.start(self.alice)
        self.assertTrue(authz.can_view_attempt(self.alice, attempt))
        self.assertTrue(authz.can_answer_attempt(self.alice, attempt))
        self.assertTrue(authz.can_submit_attempt(self.alice, attempt))
        for other in (self.bob, self.lect1, self.app_admin, self.superuser):
            with self.subTest(user=other.username):
                self.assertFalse(authz.can_view_attempt(other, attempt))
                self.assertFalse(authz.can_answer_attempt(other, attempt))
                self.assertFalse(authz.can_submit_attempt(other, attempt))

    def test_answering_stops_at_the_deadline_but_submitting_stays_possible(self):
        attempt = self.start(self.alice)
        late = attempt.deadline
        self.assertFalse(authz.can_answer_attempt(self.alice, attempt, late))
        self.assertTrue(authz.can_submit_attempt(self.alice, attempt))  # settled as EXPIRED by the service

    def test_listing_querysets_mirror_the_decisions(self):
        courses = list(Course.objects.all())
        users = [self.alice, self.bob, self.carol, self.dave, self.lect1, self.lect2, self.app_admin, self.superuser]
        for user in users:
            managed = set(authz.courses_managed_by(user).values_list("pk", flat=True))
            accessible = set(authz.courses_accessible_to_student(user).values_list("pk", flat=True))
            for course in courses:
                with self.subTest(user=user.username, course=course.code):
                    self.assertEqual(course.pk in managed, authz.can_manage_course(user, course))
                    self.assertEqual(course.pk in accessible, authz._student_can_access_course(user, course))


# --------------------------------------------------------------------------------
# LECTURER WORKFLOW
# --------------------------------------------------------------------------------
class LecturerWorkflowTests(AssessmentTestCase):
    def test_full_lecturer_flow(self):
        self.login(self.lect1)
        response = self.client.post(reverse("assessments:test_create", args=[self.cyb201.pk]), test_data())
        test = Test.objects.get(title="Quiz 1")
        self.assertRedirects(response, reverse("assessments:test_manage", args=[test.pk]))
        self.assertEqual((test.status, test.creator, test.course), ("DRAFT", self.lect1, self.cyb201))
        self.assertIsNone(test.published_at)

        add = reverse("assessments:question_add", args=[test.pk])
        self.assertEqual(self.client.post(add, question_data()).status_code, 302)
        self.assertEqual(self.client.post(add, question_data(text="Second", correct="1")).status_code, 302)
        q = test.questions.get(order=1)
        self.assertEqual([c.text for c in q.choices.all()], ["3", "4", "5"])
        self.assertEqual(q.choices.get(is_correct=True).text, "4")

        self.client.post(reverse("assessments:test_publish", args=[test.pk]))
        test.refresh_from_db()
        self.assertEqual(test.status, "PUBLISHED")
        self.assertIsNotNone(test.published_at)

    def test_forged_server_controlled_fields_are_ignored_on_create(self):
        self.login(self.lect1)
        self.client.post(
            reverse("assessments:test_create", args=[self.cyb201.pk]),
            test_data(course=self.cyb205.pk, creator=self.lect2.pk, status="PUBLISHED", published_at="2020-01-01 00:00"),
        )
        test = Test.objects.get(title="Quiz 1")
        self.assertEqual((test.course, test.creator, test.status, test.published_at), (self.cyb201, self.lect1, "DRAFT", None))

    def test_cannot_create_test_in_a_course_you_do_not_manage(self):
        before = self.db_state()
        for actor in (self.lect2, self.alice, self.carol):
            self.login(actor)
            url = reverse("assessments:test_create", args=[self.cyb201.pk])
            with self.subTest(actor=actor.username):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, test_data(title="Hack")).status_code, 404)
        self.assertEqual(self.db_state(), before)
        self.assertFalse(Test.objects.filter(title="Hack").exists())

    def test_cannot_create_test_in_inactive_course_or_missing_course(self):
        self.login(self.lect1)
        self.assertEqual(self.client.post(reverse("assessments:test_create", args=[self.inactive.pk]), test_data()).status_code, 404)
        self.assertEqual(self.client.post(reverse("assessments:test_create", args=[99999]), test_data()).status_code, 404)

    def test_admin_may_create_tests_in_any_course(self):
        self.login(self.app_admin)
        self.assertEqual(self.client.post(reverse("assessments:test_create", args=[self.cyb205.pk]), test_data()).status_code, 302)
        self.assertEqual(Test.objects.get(title="Quiz 1").creator, self.app_admin)

    def test_invalid_test_forms_are_rejected(self):
        self.login(self.lect1)
        url = reverse("assessments:test_create", args=[self.cyb201.pk])
        now = timezone.now()
        bad = [
            test_data(duration_minutes="0"),
            test_data(duration_minutes="601"),
            test_data(duration_minutes="abc"),
            test_data(title="   "),
            test_data(opens_at=(now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                      closes_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")),
            test_data(closes_at=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")),
        ]
        for data in bad:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertFalse(Test.objects.filter(title__in=["Quiz 1", "   "]).exists())

    def test_invalid_question_forms_are_rejected(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, marks=(), title="D")
        self.login(self.lect1)
        url = reverse("assessments:question_add", args=[draft.pk])
        bad = [
            question_data(choice_2="", choice_3=""),  # one choice
            question_data(correct="4"),  # correct points at a blank choice
            question_data(correct=""),  # none correct
            question_data(correct="9"),
            question_data(choice_2="3"),  # duplicate choices
            question_data(marks="0"),
            question_data(text="  "),
        ]
        for data in bad:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(draft.questions.count(), 0)

    def test_blank_middle_choice_keeps_the_correct_answer_attached_to_its_text(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, marks=(), title="D")
        self.login(self.lect1)
        self.client.post(
            reverse("assessments:question_add", args=[draft.pk]),
            question_data(choice_1="A-text", choice_2="B-text", choice_3="", choice_4="D-text", correct="4"),
        )
        q = draft.questions.get()
        self.assertEqual(q.choices.get(is_correct=True).text, "D-text")
        self.assertEqual([c.order for c in q.choices.all()], [1, 2, 3])

    def test_edit_and_delete_question_in_draft(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, marks=(1,), title="D")
        q = draft.questions.get()
        self.login(self.lect1)
        self.client.post(
            reverse("assessments:question_edit", args=[draft.pk, q.pk]),
            question_data(text="Edited", choice_1="x", choice_2="y", choice_3="", correct="1"),
        )
        q.refresh_from_db()
        self.assertEqual(q.text, "Edited")
        self.assertEqual([(c.text, c.is_correct) for c in q.choices.all()], [("x", True), ("y", False)])
        self.client.post(reverse("assessments:question_delete", args=[draft.pk, q.pk]))
        self.assertFalse(Question.objects.filter(pk=q.pk).exists())

    def test_publish_rules(self):
        self.login(self.lect1)
        empty = build_test(self.cyb201, self.lect1, publish=False, marks=(), title="Empty")
        publish = reverse("assessments:test_publish", args=[empty.pk])
        self.client.post(publish)
        empty.refresh_from_db()
        self.assertEqual(empty.status, "DRAFT")
        self.assertIn("Add at least one question.", services.validate_publishable(empty))

        # a question with a single choice, one with no correct choice
        thin = build_test(self.cyb201, self.lect1, publish=False, marks=(1,), title="Thin")
        thin.questions.get().choices.filter(order__gt=1).delete()
        self.assertTrue(any("at least two choices" in p for p in services.validate_publishable(thin)))
        nokey = build_test(self.cyb201, self.lect1, publish=False, marks=(1,), title="NoKey")
        Choice.objects.filter(question__test=nokey).update(is_correct=False)
        self.assertTrue(any("exactly one correct" in p for p in services.validate_publishable(nokey)))
        with self.assertRaises(services.PublishError):
            services.publish_test(nokey)
        nokey.refresh_from_db()
        self.assertEqual(nokey.status, "DRAFT")

    def test_publish_rejects_a_window_that_already_closed(self):
        t = build_test(self.cyb201, self.lect1, publish=False, title="W")
        Test.objects.filter(pk=t.pk).update(closes_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaises(services.PublishError):
            services.publish_test(Test.objects.get(pk=t.pk))

    def test_published_test_is_frozen(self):
        self.login(self.lect1)
        before = self.db_state()
        q = self.q1
        t = self.test201
        attempts = [
            ("assessments:test_edit", [t.pk], test_data(title="Changed")),
            ("assessments:question_add", [t.pk], question_data()),
            ("assessments:question_edit", [t.pk, q.pk], question_data(text="Changed")),
            ("assessments:question_delete", [t.pk, q.pk], {}),
            ("assessments:test_publish", [t.pk], {}),
        ]
        for name, args, data in attempts:
            with self.subTest(route=name):
                response = self.client.post(reverse(name, args=args), data)
                self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_state(), before)

    def test_service_layer_also_refuses_edits_after_publish(self):
        with self.assertRaises(services.StateError):
            services.create_question(self.test201, {"text": "x", "marks": 1, "choices": ["a", "b"], "correct": 1})
        with self.assertRaises(services.StateError):
            services.update_question(self.q1, {"text": "x", "marks": 1, "choices": ["a", "b"], "correct": 1})
        with self.assertRaises(services.StateError):
            services.delete_question(self.q1)
        with self.assertRaises(services.StateError):
            services.publish_test(self.test201)

    def test_other_lecturer_cannot_touch_the_test(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, title="Mine")
        q = draft.questions.first()
        self.login(self.lect2)
        before = self.db_state()
        for name, args in [
            ("assessments:test_manage", [draft.pk]),
            ("assessments:test_edit", [draft.pk]),
            ("assessments:question_add", [draft.pk]),
            ("assessments:question_edit", [draft.pk, q.pk]),
        ]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404, name)
            if name != "assessments:test_manage":  # read-only page: POST is a 405 for everyone
                self.assertEqual(self.client.post(reverse(name, args=args), question_data()).status_code, 404, name)
        for name, args in [
            ("assessments:question_delete", [draft.pk, q.pk]),
            ("assessments:test_publish", [draft.pk]),
            ("assessments:test_archive", [draft.pk]),
        ]:
            self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 404, name)
        self.assertEqual(self.db_state(), before)

    def test_question_must_belong_to_the_test_in_the_url(self):
        mine = build_test(self.cyb201, self.lect1, publish=False, title="Mine")
        theirs = build_test(self.cyb205, self.lect2, publish=False, title="Theirs")
        foreign_q = theirs.questions.first()
        self.login(self.lect1)  # lect1 manages `mine`, not `theirs`
        before = self.db_state()
        self.assertEqual(self.client.post(reverse("assessments:question_delete", args=[mine.pk, foreign_q.pk])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse("assessments:question_edit", args=[mine.pk, foreign_q.pk]), question_data()).status_code, 404
        )
        self.assertEqual(self.db_state(), before)

    def test_archive_hides_test_from_new_students_but_not_from_existing_attempt(self):
        attempt = self.start(self.alice)
        self.login(self.lect1)
        self.client.post(reverse("assessments:test_archive", args=[self.test201.pk]))
        self.test201.refresh_from_db()
        self.assertEqual(self.test201.status, "ARCHIVED")
        self.login(self.bob)
        self.assertEqual(self.client.get(reverse("assessments:test_intro", args=[self.test201.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk])).status_code, 404)
        self.login(self.alice)
        self.assertEqual(self.client.get(self.attempt_url(attempt)).status_code, 200)  # her own attempt remains

    def test_lecturer_dashboard_and_course_page_list_tests(self):
        self.login(self.lect1)
        self.assertContains(self.client.get(self.lecturer_dashboard()), "Midterm")
        page = self.client.get(self.course_url(self.cyb201))
        self.assertContains(page, "Midterm")
        self.assertContains(page, reverse("assessments:test_create", args=[self.cyb201.pk]))
        self.login(self.lect2)
        self.assertNotContains(self.client.get(self.lecturer_dashboard()), "Midterm")


# --------------------------------------------------------------------------------
# STUDENT ACCESS
# --------------------------------------------------------------------------------
class StudentAccessTests(AssessmentTestCase):
    def test_dashboard_lists_only_eligible_published_tests(self):
        build_test(self.cyb201, self.lect1, publish=False, title="SecretDraft")
        build_test(self.cyb205, self.lect2, title="OtherCourse")  # alice is not enrolled in CYB205
        build_test(self.csc201, self.lect1, title="CscTest")
        page = self.get_as(self.alice, self.student_dashboard())
        self.assertContains(page, "Midterm")
        for hidden in ("SecretDraft", "OtherCourse", "CscTest"):
            self.assertNotContains(page, hidden)
        self.assertContains(self.get_as(self.bob, self.student_dashboard()), "OtherCourse")
        for ineligible in (self.carol, self.dave):
            self.assertNotContains(self.get_as(ineligible, self.student_dashboard()), "Midterm")

    def test_ineligible_students_get_404_everywhere(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, title="D")
        for user, test in [(self.carol, self.test201), (self.dave, self.test201), (self.alice, draft), (self.bob, build_test(self.cyb203, self.lect1, title="203"))]:
            self.login(user)
            with self.subTest(user=user.username, test=test.title):
                self.assertEqual(self.client.get(reverse("assessments:test_intro", args=[test.pk])).status_code, 404)
                self.assertEqual(self.client.post(reverse("assessments:attempt_start", args=[test.pk])).status_code, 404)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_lecturers_and_admins_see_404_on_student_pages(self):
        for user in (self.lect1, self.app_admin, self.superuser):
            self.login(user)
            self.assertEqual(self.client.get(reverse("assessments:test_intro", args=[self.test201.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk])).status_code, 404)

    def test_students_cannot_open_manage_pages(self):
        self.login(self.alice)
        for name in ("test_manage", "test_edit", "question_add"):
            self.assertEqual(self.client.get(reverse(f"assessments:{name}", args=[self.test201.pk])).status_code, 404)

    def test_unopened_and_closed_windows_block_a_new_attempt(self):
        now = timezone.now()
        self.login(self.alice)
        for fields in ({"opens_at": now + timedelta(hours=1)}, {"opens_at": None, "closes_at": now - timedelta(seconds=1)}):
            Test.objects.filter(pk=self.test201.pk).update(**fields)
            with self.subTest(fields=fields):
                response = self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Attempt.objects.count(), 0)

    def test_enrollment_removal_stops_new_starts_but_not_an_existing_attempt(self):
        attempt = self.start(self.alice)
        self.cyb201.enrollments.filter(student=self.alice.student_profile).delete()
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse("assessments:test_intro", args=[self.test201.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.attempt_url(attempt)).status_code, 200)


# --------------------------------------------------------------------------------
# ATTEMPTS, ANSWERS, SCORING
# --------------------------------------------------------------------------------
class AttemptFlowTests(AssessmentTestCase):
    def test_start_creates_one_attempt_with_a_server_deadline(self):
        self.login(self.alice)
        url = reverse("assessments:attempt_start", args=[self.test201.pk])
        first = self.client.post(url)
        attempt = Attempt.objects.get()
        self.assertRedirects(first, self.attempt_url(attempt))
        self.assertEqual(attempt.student, self.alice.student_profile)
        self.assertEqual(attempt.status, "IN_PROGRESS")
        self.assertEqual(attempt.deadline - attempt.started_at, timedelta(minutes=30))
        self.assertIsNone(attempt.score)
        # starting again resumes the same attempt instead of creating another
        self.assertRedirects(self.client.post(url), self.attempt_url(attempt))
        self.assertEqual(Attempt.objects.count(), 1)

    def test_start_service_is_idempotent(self):
        profile = self.alice.student_profile
        a, created = services.start_attempt(profile, self.test201)
        b, created_again = services.start_attempt(profile, self.test201)
        self.assertEqual((a.pk, created, created_again), (b.pk, True, False))

    def test_attempt_page_hides_the_answer_key(self):
        attempt = self.start(self.alice)
        page = self.client.get(self.attempt_url(attempt))
        self.assertEqual(page.status_code, 200)
        for token in ("is_correct", "correct", "&#10003;"):
            self.assertNotContains(page, token)
        for question in page.context["questions"]:
            for choice in question.choices.all():
                self.assertIn("is_correct", choice.get_deferred_fields())  # never even loaded

    def test_full_student_flow_scores_on_the_server(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1), self.q2: wrong(self.q2)})
        response = self.answer(self.alice, attempt, {}, name="attempt_submit")
        self.assertRedirects(response, self.attempt_url(attempt))
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.score, attempt.max_score), ("SUBMITTED", 2, 5))
        self.assertIsNotNone(attempt.submitted_at)
        page = self.client.get(self.attempt_url(attempt))
        self.assertContains(page, "2 / 5")
        self.assertContains(page, "40.0%")
        self.assertNotContains(page, "is_correct")

    def test_marks_are_weighted_unanswered_scores_zero_and_full_marks_work(self):
        a = self.start(self.alice)
        self.answer(self.alice, a, {self.q2: right(self.q2)}, name="attempt_submit")
        a.refresh_from_db()
        self.assertEqual((a.score, a.max_score), (3, 5))  # q1 unanswered
        b = self.start(self.bob)
        self.answer(self.bob, b, {self.q1: right(self.q1), self.q2: right(self.q2)}, name="attempt_submit")
        b.refresh_from_db()
        self.assertEqual((b.score, b.max_score), (5, 5))
        c = self.start(make_student("erin", self.cyb, self.l200, [self.cyb201]))
        services.submit_attempt(c)
        c.refresh_from_db()
        self.assertEqual((c.score, c.max_score), (0, 5))

    def test_changing_an_answer_replaces_it(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: wrong(self.q1)})
        self.answer(self.alice, attempt, {self.q1: right(self.q1)})
        self.assertEqual(StudentAnswer.objects.filter(attempt=attempt).count(), 1)
        services.submit_attempt(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 2)

    def test_answers_sent_with_submit_are_counted(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1), self.q2: right(self.q2)}, name="attempt_submit")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 5)

    def test_double_submit_changes_nothing(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1)}, name="attempt_submit")
        attempt.refresh_from_db()
        snapshot = (attempt.status, attempt.score, attempt.submitted_at)
        # the second submit even carries better answers: they must not count
        response = self.answer(self.alice, attempt, {self.q1: right(self.q1), self.q2: right(self.q2)}, name="attempt_submit")
        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.score, attempt.submitted_at), snapshot)
        self.assertEqual(StudentAnswer.objects.filter(attempt=attempt).count(), 1)

    def test_cannot_answer_after_submit(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {}, name="attempt_submit")
        self.answer(self.alice, attempt, {self.q1: right(self.q1)})
        self.assertEqual(StudentAnswer.objects.count(), 0)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)

    def test_ajax_save_responses(self):
        attempt = self.start(self.alice)
        ok = self.answer(self.alice, attempt, {self.q1: right(self.q1)}, ajax="1")
        self.assertEqual(ok.status_code, 204)
        services.submit_attempt(attempt)
        late = self.answer(self.alice, attempt, {self.q2: right(self.q2)}, ajax="1")
        self.assertEqual(late.status_code, 409)

    def test_results_page_for_in_progress_attempt_is_the_take_page_and_submit_page_is_post_only(self):
        attempt = self.start(self.alice)
        self.assertEqual(self.client.get(self.attempt_url(attempt, "attempt_submit")).status_code, 405)
        self.assertEqual(self.client.get(self.attempt_url(attempt, "attempt_save")).status_code, 405)


# --------------------------------------------------------------------------------
# SERVER-SIDE TIMING
# --------------------------------------------------------------------------------
class TimingTests(AssessmentTestCase):
    def expire(self, attempt, by=timedelta(seconds=1)):
        """Move the clock past the deadline by rewriting the stored times (what waiting would do)."""
        now = timezone.now()
        Attempt.objects.filter(pk=attempt.pk).update(started_at=now - timedelta(minutes=31), deadline=now - by)

    def test_answers_after_the_deadline_are_rejected(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1)})  # in time
        self.expire(attempt)
        response = self.answer(self.alice, attempt, {self.q2: right(self.q2)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(StudentAnswer.objects.values_list("question_id", flat=True)), [self.q1.pk])
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.score, attempt.submitted_at), ("EXPIRED", 2, None))

    def test_submit_after_the_deadline_is_expired_and_ignores_late_answers(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1)})
        self.expire(attempt)
        self.answer(self.alice, attempt, {self.q2: right(self.q2)}, name="attempt_submit")
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.score, attempt.max_score), ("EXPIRED", 2, 5))
        self.assertIsNone(attempt.submitted_at)
        self.assertEqual(StudentAnswer.objects.count(), 1)

    def test_service_boundary_exactly_at_the_deadline_counts_as_late(self):
        attempt = self.start(self.alice)
        just_before = attempt.deadline - timedelta(microseconds=1)
        services.record_answers(attempt, {self.q1.pk: right(self.q1).pk}, now=just_before)
        with self.assertRaises(services.StateError):
            services.record_answers(attempt, {self.q2.pk: right(self.q2).pk}, now=attempt.deadline)
        final = services.submit_attempt(attempt, now=attempt.deadline)
        self.assertEqual(final.status, "EXPIRED")

    def test_opening_an_overdue_attempt_expires_it_lazily(self):
        attempt = self.start(self.alice)
        self.expire(attempt)
        page = self.client.get(self.attempt_url(attempt))
        self.assertContains(page, "Expired")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "EXPIRED")

    def test_dashboard_expires_overdue_attempts(self):
        attempt = self.start(self.alice)
        self.expire(attempt)
        self.client.get(self.student_dashboard())
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "EXPIRED")

    def test_deadline_is_fixed_at_start_and_unaffected_by_later_duration_edits(self):
        attempt = self.start(self.alice)
        Test.objects.filter(pk=self.test201.pk).update(duration_minutes=1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.deadline - attempt.started_at, timedelta(minutes=30))

    def test_expired_attempt_cannot_be_restarted(self):
        attempt = self.start(self.alice)
        self.expire(attempt)
        self.client.get(self.attempt_url(attempt))
        self.client.post(reverse("assessments:attempt_start", args=[self.test201.pk]))
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertEqual(Attempt.objects.get().status, "EXPIRED")

    def test_take_page_shows_remaining_time_from_the_server(self):
        attempt = self.start(self.alice)
        page = self.client.get(self.attempt_url(attempt))
        remaining = page.context["remaining_seconds"]
        self.assertTrue(1790 <= remaining <= 1800)


# --------------------------------------------------------------------------------
# ATTACKS
# --------------------------------------------------------------------------------
class AttackTests(AssessmentTestCase):
    def test_forged_fields_on_start_are_ignored(self):
        self.login(self.alice)
        self.client.post(
            reverse("assessments:attempt_start", args=[self.test201.pk]),
            {
                "student": self.bob.student_profile.pk, "student_id": self.bob.student_profile.pk,
                "test_id": 999, "status": "SUBMITTED", "score": 100, "max_score": 100,
                "started_at": "2000-01-01 00:00", "deadline": "2999-01-01 00:00", "submitted_at": "2000-01-02 00:00",
            },
        )
        a = Attempt.objects.get()
        self.assertEqual((a.student, a.test, a.status, a.score), (self.alice.student_profile, self.test201, "IN_PROGRESS", None))
        self.assertLess(abs((timezone.now() - a.started_at).total_seconds()), 60)
        self.assertEqual(a.deadline - a.started_at, timedelta(minutes=30))

    def test_forged_score_and_status_on_submit_are_ignored(self):
        attempt = self.start(self.alice)
        self.answer(
            self.alice, attempt, {self.q1: wrong(self.q1)}, name="attempt_submit",
            score=5, max_score=5, status="SUBMITTED", is_correct="true", correct_answers=2,
            percentage=100, submitted_at="2000-01-01", started_at="2000-01-01", deadline="2999-01-01",
        )
        attempt.refresh_from_db()
        self.assertEqual((attempt.score, attempt.max_score), (0, 5))
        self.assertGreater(attempt.started_at.year, 2000)
        self.assertLess(attempt.deadline.year, 2999)

    def test_other_students_attempt_is_invisible_and_untouchable(self):
        attempt = self.start(self.alice)
        self.answer(self.alice, attempt, {self.q1: right(self.q1)})
        before = self.db_state()
        self.login(self.bob)
        for name, method in [("attempt_detail", "get"), ("attempt_save", "post"), ("attempt_submit", "post")]:
            with self.subTest(route=name):
                response = getattr(self.client, method)(
                    self.attempt_url(attempt, name), {f"q_{self.q2.pk}": right(self.q2).pk}
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.db_state(), before)

    def test_missing_and_foreign_attempts_are_indistinguishable(self):
        attempt = self.start(self.alice)
        self.login(self.bob)
        foreign = self.client.get(self.attempt_url(attempt))
        missing = self.client.get(reverse("assessments:attempt_detail", args=[self.test201.pk, 99999]))
        from authorization.test_boundary import masked
        self.assertEqual(masked(foreign), masked(missing))

    def test_attempt_with_the_wrong_test_id_in_the_url_is_404(self):
        attempt = self.start(self.alice)
        other = build_test(self.cyb201, self.lect1, title="Other")
        self.login(self.alice)
        for name in ("attempt_detail", "attempt_save", "attempt_submit"):
            url = reverse(f"assessments:{name}", args=[other.pk, attempt.pk])
            response = self.client.get(url) if name == "attempt_detail" else self.client.post(url)
            self.assertEqual(response.status_code, 404, name)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "IN_PROGRESS")

    def test_lecturers_and_admins_cannot_read_or_write_a_students_attempt(self):
        attempt = self.start(self.alice)
        before = self.db_state()
        for user in (self.lect1, self.app_admin, self.superuser):
            self.login(user)
            self.assertEqual(self.client.get(self.attempt_url(attempt)).status_code, 404)
            self.assertEqual(self.client.post(self.attempt_url(attempt, "attempt_submit")).status_code, 404)
            self.assertEqual(self.client.post(self.attempt_url(attempt, "attempt_save"), {f"q_{self.q1.pk}": right(self.q1).pk}).status_code, 404)
        self.assertEqual(self.db_state(), before)

    def test_answer_with_a_question_from_another_test_is_rejected(self):
        attempt = self.start(self.alice)
        other = build_test(self.cyb201, self.lect1, title="Other")
        fq = other.questions.first()
        before = self.db_state()
        self.login(self.alice)
        response = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"q_{fq.pk}": right(fq).pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db_state(), before)

    def test_answer_with_a_choice_from_another_question_is_rejected(self):
        attempt = self.start(self.alice)
        before = self.db_state()
        self.login(self.alice)
        response = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"q_{self.q1.pk}": right(self.q2).pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db_state(), before)

    def test_malformed_and_nonexistent_ids_are_rejected(self):
        attempt = self.start(self.alice)
        before = self.db_state()
        self.login(self.alice)
        for data in (
            {f"q_{self.q1.pk}": "abc"},
            {"q_abc": "1"},
            {f"q_{self.q1.pk}": ""},
            {"q_99999": "1"},
            {f"q_{self.q1.pk}": "99999"},
            {f"q_{self.q1.pk}": "-1"},
        ):
            for name in ("attempt_save", "attempt_submit"):
                with self.subTest(data=data, route=name):
                    self.assertEqual(self.client.post(self.attempt_url(attempt, name), data).status_code, 400)
        self.assertEqual(self.db_state(), before)  # a rejected submit does not finalize either

    def test_one_bad_pair_rejects_the_whole_request(self):
        attempt = self.start(self.alice)
        self.login(self.alice)
        response = self.client.post(
            self.attempt_url(attempt, "attempt_save"),
            {f"q_{self.q1.pk}": right(self.q1).pk, f"q_{self.q2.pk}": right(self.q1).pk},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StudentAnswer.objects.count(), 0)

    def test_too_many_selections_rejected(self):
        attempt = self.start(self.alice)
        data = {f"q_{i}": "1" for i in range(1, services.MAX_SELECTIONS_PER_REQUEST + 2)}
        self.assertEqual(self.client.post(self.attempt_url(attempt, "attempt_save"), data).status_code, 400)

    def test_students_cannot_author_publish_or_archive(self):
        draft = build_test(self.cyb201, self.lect1, publish=False, title="Draft")
        q = draft.questions.first()
        before = self.db_state()
        self.login(self.alice)
        for name, args in [
            ("test_publish", [draft.pk]), ("test_archive", [self.test201.pk]),
            ("question_delete", [draft.pk, q.pk]),
        ]:
            self.assertEqual(self.client.post(reverse(f"assessments:{name}", args=args)).status_code, 404)
        for name, args in [("test_edit", [draft.pk]), ("question_add", [draft.pk]), ("question_edit", [draft.pk, q.pk])]:
            self.assertEqual(self.client.post(reverse(f"assessments:{name}", args=args), question_data()).status_code, 404)
        self.assertEqual(self.client.post(reverse("assessments:test_create", args=[self.cyb201.pk]), test_data()).status_code, 404)
        self.assertEqual(self.db_state(), before)

    def test_post_only_routes_reject_other_verbs_identically_for_any_id(self):
        attempt = self.start(self.alice)
        for user in (self.alice, self.bob):
            self.login(user)
            for name, args in [
                ("attempt_start", [self.test201.pk]), ("attempt_start", [99999]),
                ("attempt_save", [self.test201.pk, attempt.pk]), ("attempt_save", [self.test201.pk, 99999]),
                ("attempt_submit", [self.test201.pk, attempt.pk]), ("attempt_submit", [self.test201.pk, 99999]),
                ("test_publish", [self.test201.pk]), ("test_publish", [99999]),
                ("test_archive", [self.test201.pk]), ("question_delete", [self.test201.pk, self.q1.pk]),
            ]:
                for method in ("get", "put", "patch", "delete"):
                    with self.subTest(user=user.username, route=name, args=args, method=method):
                        self.assertEqual(getattr(self.client, method)(reverse(f"assessments:{name}", args=args)).status_code, 405)

    def test_read_only_pages_reject_writes(self):
        attempt = self.start(self.alice)
        for name, args in [
            ("test_manage", [self.test201.pk]), ("test_intro", [self.test201.pk]), ("attempt_detail", [self.test201.pk, attempt.pk]),
        ]:
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(route=name, method=method):
                    self.assertEqual(getattr(self.client, method)(reverse(f"assessments:{name}", args=args)).status_code, 405)

    def test_csrf_is_enforced_on_every_state_changing_route(self):
        attempt = self.start(self.alice)
        draft = build_test(self.cyb201, self.lect1, publish=False, title="D")
        strict = Client(enforce_csrf_checks=True)
        posts = [
            (self.alice, reverse("assessments:attempt_start", args=[self.test201.pk]), {}),
            (self.alice, self.attempt_url(attempt, "attempt_save"), {f"q_{self.q1.pk}": right(self.q1).pk}),
            (self.alice, self.attempt_url(attempt, "attempt_submit"), {}),
            (self.lect1, reverse("assessments:test_publish", args=[draft.pk]), {}),
            (self.lect1, reverse("assessments:test_archive", args=[self.test201.pk]), {}),
            (self.lect1, reverse("assessments:question_add", args=[draft.pk]), question_data()),
            (self.lect1, reverse("assessments:test_create", args=[self.cyb201.pk]), test_data()),
        ]
        before = self.db_state()
        for user, url, data in posts:
            strict.force_login(user)
            with self.subTest(url=url):
                self.assertEqual(strict.post(url, data).status_code, 403)
        self.assertEqual(self.db_state(), before)

    def test_anonymous_users_are_sent_to_login(self):
        attempt = self.start(self.alice)
        self.client.logout()
        for url in (
            reverse("assessments:test_manage", args=[self.test201.pk]),
            reverse("assessments:test_intro", args=[self.test201.pk]),
            self.attempt_url(attempt),
            reverse("assessments:test_create", args=[self.cyb201.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response.url.startswith(reverse("accounts:login")), url)
        for url in (self.attempt_url(attempt, "attempt_submit"), reverse("assessments:attempt_start", args=[self.test201.pk])):
            response = self.client.post(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response.url.startswith(reverse("accounts:login")), url)

    def test_user_supplied_text_is_escaped(self):
        payload = "<script>alert(1)</script>"
        t = build_test(self.cyb201, self.lect1, publish=False, marks=(), title="<b>x</b>", description=payload)
        self.login(self.lect1)
        self.client.post(
            reverse("assessments:question_add", args=[t.pk]),
            question_data(text=payload, choice_1=payload, choice_2="safe", correct="2"),
        )
        manage = self.client.get(reverse("assessments:test_manage", args=[t.pk]))
        self.assertNotContains(manage, payload)
        self.assertContains(manage, "&lt;script&gt;")
        services.publish_test(t)
        attempt = self.start(self.alice, t)
        page = self.client.get(self.attempt_url(attempt))
        self.assertNotContains(page, payload)
        self.assertContains(page, "&lt;script&gt;")

    def test_templates_never_use_the_safe_filter_or_autoescape_off(self):
        from pathlib import Path
        from django.conf import settings

        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            text = path.read_text()
            self.assertNotIn("|safe", text, path)
            self.assertNotIn("autoescape off", text, path)
            self.assertNotIn("mark_safe", text, path)

    def test_student_templates_never_reference_the_answer_key(self):
        from pathlib import Path
        from django.conf import settings

        for name in ("attempt_take.html", "attempt_result.html", "test_intro.html"):
            self.assertNotIn("is_correct", Path(settings.BASE_DIR, "templates/assessments", name).read_text(), name)

    def test_role_forgery_through_assessment_forms_changes_nothing(self):
        self.login(self.alice)
        self.client.post(
            reverse("assessments:attempt_start", args=[self.test201.pk]), {"role": "LECTURER", "is_staff": "1", "group": "ADMIN"}
        )
        self.alice.refresh_from_db()
        self.assertTrue(authz.can_access_student_dashboard(self.alice))
        self.assertFalse(self.alice.is_staff)
        self.assertEqual(list(self.alice.groups.values_list("name", flat=True)), [STUDENT])
