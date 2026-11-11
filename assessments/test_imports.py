import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from docx import Document
from openpyxl import load_workbook
from openpyxl import Workbook
from unittest.mock import patch

from assessments.importers import MAX_IMPORT_QUESTIONS, ImportFormatError, parse_upload
from assessments import services
from assessments.models import Choice, ImportHistory, Question, QuestionBankQuestion, Test
from authorization.tests import AuthzTestCase


CSV_CONTENT = (
    "Question,Question Type,A,B,C,Correct Answer,Marks,Category\n"
    "What is 2 + 2?,MULTIPLE_CHOICE,3,4,5,B,2,Arithmetic\n"
).encode()


class ImportParserTests(AuthzTestCase):
    def test_xlsx_and_docx_extract_questions(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Question", "Question Type", "A", "B", "Correct Answer", "Marks", "Category"])
        sheet.append(["Choose one", "MULTIPLE_CHOICE", "first", "second", "B", 3, "Basics"])
        excel = io.BytesIO()
        workbook.save(excel)
        xlsx_question = parse_upload(SimpleUploadedFile("questions.xlsx", excel.getvalue()))[0]
        self.assertEqual((xlsx_question["correct"], xlsx_question["marks"], xlsx_question["category"]), (2, 3, "Basics"))

        document = Document()
        for line in ("1. Is this a test?", "A) Yes", "B) No", "Answer: B", "Marks: 2", "Category: Intro"):
            document.add_paragraph(line)
        word = io.BytesIO()
        document.save(word)
        docx_question = parse_upload(SimpleUploadedFile("questions.docx", word.getvalue()))[0]
        self.assertEqual(docx_question["question_type"], Question.Type.TRUE_FALSE)
        self.assertEqual((docx_question["correct"], docx_question["marks"], docx_question["category"]), (2, 2, "Intro"))

    def test_csv_extracts_options_answer_type_marks_and_category(self):
        uploaded = SimpleUploadedFile("questions.csv", CSV_CONTENT)
        question = parse_upload(uploaded)[0]
        self.assertEqual(question["text"], "What is 2 + 2?")
        self.assertEqual(question["choices"], ["3", "4", "5"])
        self.assertEqual(question["correct"], 2)
        self.assertEqual(question["question_type"], Question.Type.MULTIPLE_CHOICE)
        self.assertEqual(question["marks"], 2)
        self.assertEqual(question["category"], "Arithmetic")
        self.assertEqual(question["errors"], [])

    def test_blank_option_columns_are_ignored_and_letters_still_map(self):
        uploaded = SimpleUploadedFile(
            "sparse.csv",
            (
                "Question,A,B,C,D,E,Correct Answer\n"
                "True false question,True,False,,,,A\n"
                "Sparse columns,First,,Third,,,C\n"
            ).encode(),
        )
        true_false, sparse = parse_upload(uploaded)
        self.assertEqual((true_false["choices"], true_false["correct"], true_false["errors"]),
                         (["True", "False"], 1, []))
        self.assertEqual((sparse["choices"], sparse["correct"], sparse["errors"]),
                         (["First", "Third"], 2, []))

    def test_template_example_round_trips_through_the_importer(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Question", "Question Type", "A", "B", "C", "D", "E", "Correct Answer", "Marks", "Category"])
        sheet.append(["Example question", "MULTIPLE_CHOICE", "first", "second", "third", None, None, "B", 1, "Example"])
        output = io.BytesIO()
        workbook.save(output)
        question = parse_upload(SimpleUploadedFile("template.xlsx", output.getvalue()))[0]
        self.assertEqual((question["choices"], question["correct"], question["errors"]),
                         (["first", "second", "third"], 2, []))

    def test_moodle_xml_extracts_true_false_answer(self):
        xml = b"""<?xml version="1.0"?>
        <quiz><question type="truefalse">
          <name><text>Earth</text></name>
          <questiontext format="html"><text><![CDATA[<p>Earth orbits the Sun.</p>]]></text></questiontext>
          <answer fraction="100"><text>true</text></answer>
          <answer fraction="0"><text>false</text></answer>
        </question></quiz>"""
        question = parse_upload(SimpleUploadedFile("questions.xml", xml))[0]
        self.assertEqual(question["text"], "Earth orbits the Sun.")
        self.assertEqual(question["question_type"], Question.Type.TRUE_FALSE)
        self.assertEqual(question["choices"], ["true", "false"])
        self.assertEqual(question["correct"], 1)
        self.assertEqual(question["errors"], [])

    def test_pdf_extracts_text_questions(self):
        contents = (
            b"BT /F1 12 Tf 72 720 Td (1. Is it true?) Tj T* (A\\) True) Tj T* "
            b"(B\\) False) Tj T* (Answer: A) Tj ET"
        )
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(contents)).encode() + b" >>\nstream\n" + contents + b"\nendstream",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
        xref_offset = len(pdf)
        pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode())
        pdf.extend(
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
        )
        question = parse_upload(SimpleUploadedFile("questions.pdf", bytes(pdf)))[0]
        self.assertEqual(question["text"], "Is it true?")
        self.assertEqual(question["question_type"], Question.Type.TRUE_FALSE, question)
        self.assertEqual(question["correct"], 1)

    def test_rejects_bad_extensions_and_oversized_uploads(self):
        with self.assertRaises(ImportFormatError):
            parse_upload(SimpleUploadedFile("questions.exe", b"anything"))
        upload = SimpleUploadedFile("questions.csv", b"x" * (10 * 1024 * 1024 + 1))
        with self.assertRaises(ImportFormatError):
            parse_upload(upload)

    def test_malformed_supported_formats_return_validation_errors(self):
        samples = {
            "broken.xlsx": b"not a zip archive",
            "broken.xls": b"not an Excel workbook",
            "broken.docx": b"not a Word archive",
            "broken.pdf": b"not a PDF",
            "broken.xml": b"<quiz><question></quiz>",
            "broken.csv": b"\xff\xfe",
        }
        for filename, content in samples.items():
            with self.subTest(filename=filename), self.assertRaises(ImportFormatError):
                parse_upload(SimpleUploadedFile(filename, content))

    def test_question_limit_stays_below_default_form_field_limit(self):
        self.assertEqual(MAX_IMPORT_QUESTIONS, 80)
        csv_rows = ["Question,A,B,Correct Answer"]
        csv_rows.extend(f"Question {number},yes,no,A" for number in range(MAX_IMPORT_QUESTIONS + 1))
        with self.assertRaises(ImportFormatError):
            parse_upload(SimpleUploadedFile("too-many.csv", ("\n".join(csv_rows) + "\n").encode()))


class AssessmentImportWorkflowTests(AuthzTestCase):
    def setUp(self):
        self.client.force_login(self.lect1)
        self.url = reverse("assessments:import_assessment", args=[self.cyb201.pk])

    def upload(self, content=CSV_CONTENT, filename="questions.csv"):
        return self.client.post(
            self.url,
            {
                "action": "preview",
                "title": "Imported quiz",
                "description": "Review before saving",
                "duration_minutes": "30",
                "opens_at": "",
                "closes_at": "",
                "file": SimpleUploadedFile(filename, content),
            },
        )

    def save_preview(self, response, **overrides):
        history = response.context["history"]
        rows = response.context["questions"]
        data = {
            "action": "save",
            "import_id": str(history.pk),
            "question_count": str(len(rows)),
            "title": "Imported quiz",
            "description": "Review before saving",
            "duration_minutes": "30",
            "opens_at": "",
            "closes_at": "",
        }
        for index, row in enumerate(rows):
            data[f"question_{index}_text"] = row["text"]
            data[f"question_{index}_marks"] = str(row["marks"])
            data[f"question_{index}_category"] = row["category"]
            data[f"question_{index}_type"] = row["question_type"]
            data[f"question_{index}_correct"] = str(row["correct"])
            for choice in row["choice_fields"]:
                data[f"question_{index}_choice_{choice['letter']}"] = choice["value"]
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_import_preview_is_editable_and_saves_atomically_to_assessment_and_bank(self):
        preview = self.upload()
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(Test.objects.count(), 0)
        history = preview.context["history"]
        self.assertEqual(history.status, ImportHistory.Status.PREVIEWED)

        response = self.save_preview(
            preview,
            question_0_text="Edited: what is 2 + 2?",
            question_0_category="Foundations",
        )
        test = Test.objects.get(title="Imported quiz")
        question = test.questions.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(question.text, "Edited: what is 2 + 2?")
        self.assertEqual(question.marks, 2)
        self.assertEqual(question.choices.get(is_correct=True).text, "4")
        self.assertEqual(question.question_type, Question.Type.MULTIPLE_CHOICE)
        bank = QuestionBankQuestion.objects.get(course=self.cyb201)
        self.assertEqual((bank.creator, bank.category), (self.lect1, "Foundations"))
        history.refresh_from_db()
        self.assertEqual((history.status, history.question_count), (ImportHistory.Status.COMPLETED, 1))

    def test_duplicate_question_preview_cannot_create_a_partial_import(self):
        content = (
            "Question,A,B,Correct Answer\n"
            "Repeated question?,yes,no,A\n"
            "Repeated question?,true,false,A\n"
        ).encode()
        preview = self.upload(content)
        self.assertTrue(any("Duplicate question" in error for error in preview.context["questions"][1]["errors"]))
        response = self.save_preview(preview)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Test.objects.count(), 0)
        self.assertEqual(QuestionBankQuestion.objects.count(), 0)

    def test_invalid_upload_is_recorded_as_failed(self):
        response = self.upload(b"not,a,question,file\nvalue")
        self.assertEqual(response.status_code, 200)
        history = ImportHistory.objects.get(course=self.cyb201)
        self.assertEqual(history.status, ImportHistory.Status.FAILED)
        self.assertIn("Question and Correct Answer", history.details)

    def test_same_file_cannot_create_a_second_assessment(self):
        preview = self.upload()
        self.save_preview(preview)
        second = self.upload()
        self.assertContains(second, "already been processed")
        self.assertEqual(Test.objects.filter(course=self.cyb201).count(), 1)
        self.assertEqual(QuestionBankQuestion.objects.filter(course=self.cyb201).count(), 1)

    def test_same_file_can_reopen_an_abandoned_preview(self):
        first = self.upload()
        history_id = first.context["history"].pk
        reopened = self.upload()
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.context["history"].pk, history_id)
        self.assertEqual(ImportHistory.objects.filter(course=self.cyb201).count(), 1)

    def test_existing_course_question_is_flagged_as_duplicate(self):
        first = self.upload()
        self.save_preview(first)
        duplicate_csv = (
            "Question,A,B,Correct Answer\n"
            "What is 2 + 2?,3,4,B\n"
        ).encode()
        preview = self.upload(duplicate_csv, "second.csv")
        self.assertTrue(any("already exists" in error for error in preview.context["questions"][0]["errors"]))
        response = self.save_preview(preview)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Test.objects.filter(course=self.cyb201).count(), 1)

    def test_malformed_preview_ids_are_rejected_without_writes(self):
        response = self.client.post(self.url, {"action": "save", "import_id": "not-an-id"})
        self.assertEqual(response.status_code, 200)
        test = Test.objects.create(course=self.cyb201, creator=self.lect1, title="Draft")
        bank_page = reverse("assessments:question_bank", args=[self.cyb201.pk])
        self.assertEqual(self.client.post(bank_page, {"test_id": "not-an-id", "question_ids": ["1"]}).status_code, 404)
        self.assertEqual(test.questions.count(), 0)

    def test_import_and_bank_routes_enforce_course_ownership(self):
        self.assertEqual(
            self.client.get(reverse("assessments:import_assessment", args=[self.cyb205.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("assessments:question_bank", args=[self.cyb205.pk])).status_code, 404
        )

    def test_bank_search_filter_and_bulk_add(self):
        preview = self.upload()
        self.save_preview(preview)
        test = Test.objects.create(course=self.cyb201, creator=self.lect1, title="Another draft")
        bank = QuestionBankQuestion.objects.get(course=self.cyb201)
        bank_page = reverse("assessments:question_bank", args=[self.cyb201.pk])
        self.assertContains(self.client.get(bank_page + "?q=2%20%2B%202&category=Arithmetic"), "What is 2 + 2?")
        response = self.client.post(
            bank_page,
            {"test_id": str(test.pk), "question_ids": [str(bank.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        copied = test.questions.get()
        self.assertEqual(copied.text, bank.text)
        self.assertEqual(copied.choices.get(is_correct=True).text, bank.choices[1])

    def test_template_download_is_a_valid_excel_workbook(self):
        response = self.client.get(reverse("assessments:import_template"))
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.content), read_only=True)
        self.assertEqual(
            list(workbook.active.values)[0],
            ("Question", "Question Type", "A", "B", "C", "D", "E", "Correct Answer", "Marks", "Category"),
        )
        workbook.close()

    def test_import_transaction_rolls_back_every_record_when_bank_write_fails(self):
        preview = self.upload()
        history = preview.context["history"]
        rows = preview.context["questions"]
        cleaned = {
            "title": "Should roll back",
            "description": "",
            "duration_minutes": 30,
            "opens_at": None,
            "closes_at": None,
        }
        with patch.object(QuestionBankQuestion.objects, "create", side_effect=IntegrityError("forced audit failure")):
            with self.assertRaises(IntegrityError):
                services.save_imported_assessment(
                    course=self.cyb201,
                    creator=self.lect1,
                    cleaned=cleaned,
                    questions=rows,
                    history_id=history.pk,
                )
        self.assertFalse(Test.objects.filter(title="Should roll back").exists())
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(Choice.objects.count(), 0)
        self.assertEqual(QuestionBankQuestion.objects.count(), 0)
        history.refresh_from_db()
        self.assertEqual(history.status, ImportHistory.Status.PREVIEWED)
