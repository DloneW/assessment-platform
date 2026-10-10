"""Short-answer and file-upload questions: authoring, answering, upload safety, marking, secrecy."""

import io
import zipfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from assessments import services, uploads
from assessments.models import MAX_UPLOAD_BYTES, AnswerFile, Attempt, Question, StudentAnswer, Test
from assessments.tests import AssessmentTestCase, question_data
from authorization import services as authz
from authorization.tests import make_student, make_user
from authorization.test_boundary import masked

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"x" * 64


def make_docx(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buffer.getvalue()


def upload(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


class MixedTestCase(AssessmentTestCase):
    """A published test: MCQ (2), SHORT (6), FILE (4)."""

    def setUp(self):
        super().setUp()
        self.mixed = services.create_test(
            course=self.cyb201, creator=self.lect1, cleaned={"title": "Mixed", "duration_minutes": 30}
        )
        services.create_question(
            self.mixed, {"kind": "MCQ", "text": "MCQ", "marks": 2, "choices": ["right", "wrong"], "correct": 1}
        )
        services.create_question(
            self.mixed, {"kind": "SHORT", "text": "Explain", "marks": 6, "marking_guide": "SECRET GUIDE"}
        )
        services.create_question(self.mixed, {"kind": "FILE", "text": "Upload", "marks": 4, "marking_guide": "G2"})
        self.mixed = services.publish_test(self.mixed)
        self.mcq, self.short, self.file_q = self.mixed.questions.all()

    def attempt_for(self, user):
        return self.start(user, self.mixed)

    def finished(self, user, text="my answer", file=None):
        attempt = self.attempt_for(user)
        services.record_answers(attempt, {self.mcq.pk: self.mcq.choices.get(is_correct=True).pk}, {self.short.pk: text})
        if file is not None:
            services.save_file_answer(attempt, self.file_q.pk, file)
        return services.submit_attempt(attempt)


class UploadValidationTests(MixedTestCase):
    def check(self, name, data, ok=True):
        if ok:
            return uploads.inspect_upload(upload(name, data))
        with self.assertRaises(uploads.UploadRejected):
            uploads.inspect_upload(upload(name, data))

    def test_accepts_known_types(self):
        self.assertEqual(self.check("pic.png", PNG)[1], "image/png")
        self.assertEqual(self.check("a.pdf", PDF)[1], "application/pdf")
        self.assertTrue(self.check("essay.docx", make_docx())[1].endswith("wordprocessingml.document"))

    def test_rejects_wrong_content_or_extension(self):
        self.check("evil.png", b"<script>alert(1)</script>", ok=False)  # not an image
        self.check("pic.pdf", PNG, ok=False)  # extension does not match the bytes
        self.check("page.html", b"<html></html>", ok=False)
        self.check("x.svg", b"<svg onload=alert(1)></svg>", ok=False)
        self.check("run.exe", b"MZ" + b"\x00" * 100, ok=False)
        self.check("empty.png", b"", ok=False)

    def test_rejects_oversized_files(self):
        self.check("big.pdf", PDF + b"x" * MAX_UPLOAD_BYTES, ok=False)

    def test_rejects_macro_and_non_word_zip(self):
        self.check("macro.docx", make_docx({"word/vbaProject.bin": b"x"}), ok=False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("hello.txt", "hi")
        self.check("archive.docx", buf.getvalue(), ok=False)

    def test_filename_is_sanitised(self):
        name = self.check("../../etc/pass wd<>.png", PNG)[0]
        self.assertNotIn("/", name)
        self.assertNotIn("<", name)
        self.assertTrue(name.endswith(".png"))


class AuthoringTests(MixedTestCase):
    def post_question(self, test, **fields):
        self.login(self.lect1)
        return self.client.post(reverse("assessments:question_add", args=[test.pk]), fields)

    def draft(self):
        return services.create_test(course=self.cyb201, creator=self.lect1, cleaned={"title": "D", "duration_minutes": 5})

    def test_short_and_file_questions_need_no_choices(self):
        draft = self.draft()
        for kind in ("SHORT", "FILE"):
            r = self.post_question(draft, kind=kind, text="Q?", marks="3", marking_guide="guide")
            self.assertEqual(r.status_code, 302, kind)
        self.assertEqual(draft.questions.count(), 2)
        self.assertEqual(draft.questions.first().choices.count(), 0)

    def test_submitted_choices_are_ignored_for_manual_kinds(self):
        draft = self.draft()
        self.post_question(draft, kind="SHORT", text="Q?", marks="3", **{"choice_1": "a", "choice_2": "b", "correct": "1"})
        self.assertEqual(draft.questions.get().choices.count(), 0)

    def test_mcq_still_requires_choices_and_a_correct_one(self):
        draft = self.draft()
        self.assertEqual(self.post_question(draft, kind="MCQ", text="Q?", marks="1").status_code, 200)
        self.assertEqual(draft.questions.count(), 0)

    def test_unknown_kind_rejected(self):
        draft = self.draft()
        self.assertEqual(self.post_question(draft, kind="ESSAY", text="Q?", marks="1").status_code, 200)
        self.assertEqual(draft.questions.count(), 0)

    def test_publish_accepts_manual_questions_without_choices(self):
        self.assertTrue(self.mixed.is_published)

    def test_db_rejects_marking_guide_free_unknown_kind(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Question.objects.filter(pk=self.short.pk).update(kind="BOGUS")


class AnsweringTests(MixedTestCase):
    def test_text_answer_saved_and_replaced(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        url = self.attempt_url(attempt, "attempt_save")
        self.client.post(url, {f"t_{self.short.pk}": "first"})
        self.client.post(url, {f"t_{self.short.pk}": "second"})
        self.assertEqual(StudentAnswer.objects.get(attempt=attempt, question=self.short).text, "second")

    def test_blank_text_removes_the_answer(self):
        attempt = self.attempt_for(self.alice)
        services.record_answers(attempt, {}, {self.short.pk: "x"})
        services.record_answers(attempt, {}, {self.short.pk: ""})
        self.assertFalse(StudentAnswer.objects.filter(attempt=attempt, question=self.short).exists())

    def test_text_for_wrong_kind_is_400_and_stores_nothing(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"t_{self.mcq.pk}": "hello"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(StudentAnswer.objects.count(), 0)

    def test_choice_for_text_question_is_400(self):
        attempt = self.attempt_for(self.alice)
        choice = self.mcq.choices.first()
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"q_{self.short.pk}": choice.pk})
        self.assertEqual(r.status_code, 400)

    def test_overlong_text_is_400(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"t_{self.short.pk}": "x" * 2001})
        self.assertEqual(r.status_code, 400)

    def test_text_for_another_tests_question_is_400(self):
        other = self.q1  # belongs to test201, not self.mixed
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"t_{other.pk}": "x"})
        self.assertEqual(r.status_code, 400)

    def test_file_upload_through_the_form(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"f_{self.file_q.pk}": upload("d.pdf", PDF)})
        self.assertEqual(r.status_code, 302)
        stored = AnswerFile.objects.get()
        self.assertEqual(bytes(stored.content), PDF)
        self.assertEqual(stored.answer.attempt, attempt)

    def test_bad_file_is_refused_and_nothing_stored(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        self.client.post(self.attempt_url(attempt, "attempt_save"), {f"f_{self.file_q.pk}": upload("x.png", b"<html>")})
        self.assertEqual(AnswerFile.objects.count(), 0)

    def test_bad_file_blocks_submit(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        self.client.post(self.attempt_url(attempt, "attempt_submit"), {f"f_{self.file_q.pk}": upload("x.png", b"<html>")})
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_in_progress)

    def test_file_for_non_file_question_is_400(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(self.attempt_url(attempt, "attempt_save"), {f"f_{self.short.pk}": upload("d.pdf", PDF)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(AnswerFile.objects.count(), 0)

    def test_replacing_a_file_keeps_one_row(self):
        attempt = self.attempt_for(self.alice)
        services.save_file_answer(attempt, self.file_q.pk, upload("a.pdf", PDF))
        services.save_file_answer(attempt, self.file_q.pk, upload("b.png", PNG))
        self.assertEqual(AnswerFile.objects.count(), 1)
        self.assertEqual(AnswerFile.objects.get().filename, "b.png")

    def test_no_uploads_after_deadline_or_submit(self):
        attempt = self.attempt_for(self.alice)
        late = attempt.deadline + timedelta(seconds=1)
        with self.assertRaises(services.StateError):
            services.save_file_answer(attempt, self.file_q.pk, upload("a.pdf", PDF), now=late)
        services.submit_attempt(attempt)
        with self.assertRaises(services.StateError):
            services.save_file_answer(attempt, self.file_q.pk, upload("a.pdf", PDF))
        self.assertEqual(AnswerFile.objects.count(), 0)

    def test_oversize_request_rejected_before_parsing(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        r = self.client.post(
            self.attempt_url(attempt, "attempt_save"), {f"f_{self.file_q.pk}": upload("a.pdf", PDF + b"x" * (6 * MAX_UPLOAD_BYTES))}
        )
        self.assertEqual(r.status_code, 413)


class ScoringAndMarkingTests(MixedTestCase):
    def test_manual_answers_score_zero_until_marked(self):
        attempt = self.finished(self.alice)
        self.assertEqual((attempt.score, attempt.max_score), (2, 12))
        self.assertEqual(services.pending_grading_count(attempt), 1)  # short answered; file not answered

    def test_marking_updates_the_score(self):
        attempt = self.finished(self.alice, file=upload("a.pdf", PDF))
        answers = {a.question_id: a for a in attempt.answers.all()}
        graded = services.grade_attempt(
            attempt, {answers[self.short.pk].pk: (5, "good"), answers[self.file_q.pk].pk: (3, "")}, self.lect1
        )
        self.assertEqual(graded.score, 2 + 5 + 3)
        a = StudentAnswer.objects.get(pk=answers[self.short.pk].pk)
        self.assertEqual((a.awarded_marks, a.feedback, a.graded_by), (5, "good", self.lect1))

    def test_remarking_overwrites(self):
        attempt = self.finished(self.alice)
        a = attempt.answers.get(question=self.short)
        services.grade_attempt(attempt, {a.pk: (6, "")}, self.lect1)
        graded = services.grade_attempt(attempt, {a.pk: (1, "re-marked")}, self.lect1)
        self.assertEqual(graded.score, 3)

    def test_marks_out_of_range_rejected_and_nothing_changes(self):
        attempt = self.finished(self.alice)
        a = attempt.answers.get(question=self.short)
        for bad in (7, -1):
            with self.assertRaises(services.InvalidAnswer):
                services.grade_attempt(attempt, {a.pk: (bad, "")}, self.lect1)
        a.refresh_from_db()
        self.assertIsNone(a.awarded_marks)

    def test_cannot_mark_an_mcq_answer(self):
        attempt = self.finished(self.alice)
        a = attempt.answers.get(question=self.mcq)
        with self.assertRaises(services.InvalidAnswer):
            services.grade_attempt(attempt, {a.pk: (2, "")}, self.lect1)

    def test_cannot_mark_another_attempts_answer(self):
        mine = self.finished(self.alice)
        theirs = self.finished(self.bob)
        a = theirs.answers.get(question=self.short)
        with self.assertRaises(services.InvalidAnswer):
            services.grade_attempt(mine, {a.pk: (1, "")}, self.lect1)

    def test_cannot_mark_while_in_progress(self):
        attempt = self.attempt_for(self.alice)
        services.record_answers(attempt, {}, {self.short.pk: "x"})
        a = attempt.answers.get()
        with self.assertRaises(services.StateError):
            services.grade_attempt(attempt, {a.pk: (1, "")}, self.lect1)

    def test_db_requires_marks_and_graded_at_together(self):
        from django.db import IntegrityError, transaction

        attempt = self.finished(self.alice)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentAnswer.objects.filter(attempt=attempt, question=self.short).update(awarded_marks=3)

    def test_marking_view_flow(self):
        attempt = self.finished(self.alice)
        a = attempt.answers.get(question=self.short)
        self.login(self.lect1)
        url = reverse("assessments:grade", args=[self.mixed.pk, attempt.pk])
        page = self.client.get(url)
        self.assertContains(page, "SECRET GUIDE")  # lecturers see the marking guide
        r = self.client.post(url, {f"marks_{a.pk}": "4", f"feedback_{a.pk}": "ok"})
        self.assertEqual(r.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 6)

    def test_marker_identity_comes_from_the_session(self):
        attempt = self.finished(self.alice)
        a = attempt.answers.get(question=self.short)
        self.login(self.lect1)
        self.client.post(
            reverse("assessments:grade", args=[self.mixed.pk, attempt.pk]),
            {f"marks_{a.pk}": "4", "graded_by": str(self.bob.pk), "score": "99"},
        )
        a.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(a.graded_by, self.lect1)
        self.assertEqual(attempt.score, 6)


class AccessTests(MixedTestCase):
    def setUp(self):
        super().setUp()
        self.attempt = self.finished(self.alice, file=upload("a.pdf", PDF))
        self.answer = self.attempt.answers.get(question=self.file_q)
        self.dl = reverse("assessments:answer_file", args=[self.mixed.pk, self.attempt.pk, self.answer.pk])
        self.grade_url = reverse("assessments:grade", args=[self.mixed.pk, self.attempt.pk])
        self.subs_url = reverse("assessments:submissions", args=[self.mixed.pk])

    def test_owner_and_course_lecturer_can_download(self):
        for user in (self.alice, self.lect1):
            self.login(user)
            r = self.client.get(self.dl)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, PDF)
            self.assertIn("attachment", r["Content-Disposition"])
            self.assertEqual(r["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", r["Content-Security-Policy"])

    def test_everyone_else_gets_the_same_404(self):
        missing = reverse("assessments:answer_file", args=[self.mixed.pk, self.attempt.pk, 99999])
        self.login(self.bob)
        baseline = masked(self.client.get(missing))
        for user in (self.bob, self.lect2):
            self.login(user)
            for url in (self.dl, self.grade_url, self.subs_url):
                self.assertEqual(self.client.get(url).status_code, 404, (user, url))
        self.login(self.bob)
        self.assertEqual(masked(self.client.get(self.dl)), baseline)

    def test_students_cannot_mark(self):
        self.login(self.alice)
        a = self.attempt.answers.get(question=self.short)
        r = self.client.post(self.grade_url, {f"marks_{a.pk}": "6"})
        self.assertEqual(r.status_code, 404)
        a.refresh_from_db()
        self.assertIsNone(a.awarded_marks)

    def test_file_is_not_reachable_through_another_attempt(self):
        other = self.finished(self.bob)
        url = reverse("assessments:answer_file", args=[self.mixed.pk, other.pk, self.answer.pk])
        self.login(self.lect1)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_lecturer_listing_shows_pending(self):
        self.login(self.lect1)
        self.assertContains(self.client.get(self.subs_url), "to mark")

    def test_authz_checks(self):
        self.assertTrue(authz.can_grade_attempt(self.lect1, self.attempt))
        self.assertFalse(authz.can_grade_attempt(self.lect2, self.attempt))
        self.assertFalse(authz.can_grade_attempt(self.alice, self.attempt))
        self.assertFalse(authz.can_download_answer_file(self.bob, self.attempt))


class SecrecyTests(MixedTestCase):
    def test_students_never_see_the_marking_guide(self):
        attempt = self.attempt_for(self.alice)
        self.login(self.alice)
        self.assertNotContains(self.client.get(self.attempt_url(attempt)), "SECRET GUIDE")
        services.submit_attempt(attempt)
        self.assertNotContains(self.client.get(self.attempt_url(attempt)), "SECRET GUIDE")

    def test_result_page_shows_marks_and_feedback(self):
        attempt = self.finished(self.alice, text="<b>mine</b>")
        a = attempt.answers.get(question=self.short)
        services.grade_attempt(attempt, {a.pk: (4, "well done")}, self.lect1)
        self.login(self.alice)
        page = self.client.get(self.attempt_url(attempt))
        self.assertContains(page, "well done")
        self.assertContains(page, "&lt;b&gt;mine&lt;/b&gt;")  # escaped, not rendered


class SampleCommandTests(AssessmentTestCase):
    def setUp(self):
        super().setUp()
        self.cyb201.title = "Introduction to Cybersecurity and Strategy"
        self.cyb201.save()
        self.cyb201.lecturers.add(self.lect1)

    def run_cmd(self, *args):
        call_command("load_sample_test", *args, stdout=io.StringIO())

    def test_loads_60_marks_with_all_three_kinds(self):
        self.run_cmd()
        test = Test.objects.get(course=self.cyb201, title__startswith="Introduction to Cybersecurity")
        kinds = set(test.questions.values_list("kind", flat=True))
        self.assertEqual(kinds, {"MCQ", "SHORT", "FILE"})
        self.assertEqual(sum(test.questions.values_list("marks", flat=True)), 60)
        self.assertTrue(test.is_draft)

    def test_idempotent_and_can_publish(self):
        self.run_cmd("--publish")
        self.run_cmd("--publish")
        self.assertEqual(Test.objects.filter(title__startswith="Introduction to Cybersecurity").count(), 1)
        self.assertTrue(Test.objects.get(title__startswith="Introduction to Cybersecurity").is_published)

    def test_unknown_course_and_no_lecturer(self):
        with self.assertRaises(CommandError):
            self.run_cmd("--course-code", "NOPE999")
        self.cyb201.lecturers.clear()
        with self.assertRaises(CommandError):
            self.run_cmd()
