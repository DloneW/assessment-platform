"""Parsers for the supported lecturer question-import formats."""

import csv
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from .models import Question

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_QUESTIONS = 80
MAX_IMPORT_ROWS = 5000
MAX_CHOICES = 5
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".docx", ".pdf", ".xml"}


class ImportFormatError(Exception):
    """The uploaded file is invalid or cannot be read as a supported format."""


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _integer(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _record(text, choices, correct, marks=1, category="", question_type=Question.Type.MULTIPLE_CHOICE):
    text = _clean(text)
    choices = [_clean(choice) for choice in choices]
    errors = []
    marks = _integer(marks) or 0
    correct = _integer(correct) or 0
    if not text:
        errors.append("Question text is required.")
    if len(text) > 2000:
        errors.append("Question text cannot exceed 2,000 characters.")
    if len(_clean(category)) > 100:
        errors.append("Category cannot exceed 100 characters.")
    if not 2 <= len([choice for choice in choices if choice]) <= MAX_CHOICES:
        errors.append(f"Provide 2 to {MAX_CHOICES} non-empty answer options.")
    if any(len(choice) > 500 for choice in choices):
        errors.append("Answer options cannot exceed 500 characters.")
    if any(not choice for choice in choices):
        errors.append("Answer options cannot contain blanks.")
    if len({choice.casefold() for choice in choices}) != len(choices):
        errors.append("Answer options must be different.")
    if not 1 <= marks <= 100:
        errors.append("Marks must be between 1 and 100.")
    if not 1 <= correct <= len(choices):
        errors.append("Correct answer must identify one of the answer options.")
    if question_type not in Question.Type.values:
        errors.append(f"Unsupported question type: {question_type}.")
    elif question_type == Question.Type.TRUE_FALSE and len(choices) != 2:
        errors.append("True / false questions need exactly two options.")
    return {
        "text": text,
        "choices": choices[:MAX_CHOICES],
        "correct": correct,
        "marks": marks,
        "category": _clean(category),
        "question_type": question_type,
        "errors": errors,
    }


def _header_key(value):
    return re.sub(r"[^a-z0-9]+", "_", _clean(value).casefold()).strip("_")


def _from_table(rows):
    rows = iter(rows)
    try:
        header_row = next(rows)
    except StopIteration:
        raise ImportFormatError("The file contains no rows.")
    headers = [_header_key(value) for value in header_row]
    aliases = {
        "question": ("question", "question_text", "text"),
        "correct": ("correct", "correct_answer", "answer", "answer_key"),
        "marks": ("marks", "mark", "points"),
        "category": ("category", "topic"),
        "question_type": ("question_type", "type"),
    }

    def find_header(names):
        return next((headers.index(name) for name in names if name in headers), None)

    question_col = find_header(aliases["question"])
    correct_col = find_header(aliases["correct"])
    if question_col is None or correct_col is None:
        raise ImportFormatError("Include Question and Correct Answer columns in the first row.")
    choice_cols = []
    for letter in "abcde":
        position = find_header((letter, f"choice_{letter}", f"option_{letter}", f"answer_{letter}"))
        if position is not None:
            choice_cols.append((letter, position))
    if len(choice_cols) < 2:
        raise ImportFormatError("Include at least two answer-option columns (A, B, ...).")
    marks_col = find_header(aliases["marks"])
    category_col = find_header(aliases["category"])
    type_col = find_header(aliases["question_type"])
    records = []
    for number, row in enumerate(rows, start=2):
        if number > MAX_IMPORT_ROWS:
            raise ImportFormatError(f"Spreadsheet imports are limited to {MAX_IMPORT_ROWS} rows.")
        values = list(row)
        if not any(_clean(value) for value in values):
            continue
        get = lambda index: values[index] if index is not None and index < len(values) else ""
        source_choices = [(letter, _clean(get(index))) for letter, index in choice_cols]
        option_values = [value for _, value in source_choices if value]
        raw_correct = _clean(get(correct_col))
        correct = None
        if raw_correct:
            correct_letter = raw_correct.upper()
            if correct_letter in "ABCDE" and len(correct_letter) == 1:
                letter_choices = [
                    value for letter, value in source_choices if value and letter.upper() <= correct_letter
                ]
                if any(letter.upper() == correct_letter and value for letter, value in source_choices):
                    correct = len(letter_choices)
            else:
                for position, option in enumerate(option_values, start=1):
                    if option.casefold() == raw_correct.casefold():
                        correct = position
                        break
                if correct is None:
                    correct = _integer(raw_correct)
        raw_type = _header_key(get(type_col))
        question_type = {
            "": Question.Type.MULTIPLE_CHOICE,
            "mcq": Question.Type.MULTIPLE_CHOICE,
            "multiple_choice": Question.Type.MULTIPLE_CHOICE,
            "truefalse": Question.Type.TRUE_FALSE,
            "true_false": Question.Type.TRUE_FALSE,
            "tf": Question.Type.TRUE_FALSE,
        }.get(raw_type, raw_type.upper())
        raw_marks = get(marks_col)
        records.append(
            _record(
                get(question_col),
                option_values,
                correct,
                1 if raw_marks in ("", None) else raw_marks,
                get(category_col),
                question_type,
            )
        )
        if records[-1]["errors"]:
            records[-1]["errors"].insert(0, f"Row {number}:")
        if len(records) > MAX_IMPORT_QUESTIONS:
            raise ImportFormatError(f"An import may contain no more than {MAX_IMPORT_QUESTIONS} questions.")
    if not records:
        raise ImportFormatError("No question rows were found in the spreadsheet.")
    return records


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def text(self, markup):
        self.parts = []
        self.feed(markup or "")
        return " ".join(self.parts).strip()


def _text_questions(text):
    records = []
    current = None
    lines = []
    for raw_line in text.splitlines():
        lines.extend(
            part.strip()
            for part in re.split(
                r"(?<=\s)(?=[A-E][\).]\s)|(?<=[^\s])(?=[A-E][\).]\s)|"
                r"(?<=\s)(?=(?:correct\s+)?answer\s*:)|(?<=[^\s])(?=(?:correct\s+)?answer\s*:)|"
                r"(?<=\s)(?=(?:marks?|points?|category)\s*:)|(?<=[^\s])(?=(?:marks?|points?|category)\s*:)",
                raw_line,
                flags=re.IGNORECASE,
            )
        )
    for line in lines:
        if not line:
            continue
        question_match = re.match(r"^(?:(?:question|q)\s*)?\d+\s*[.):]\s*(.+)$", line, re.I)
        if question_match:
            if current:
                records.append(_record(**current))
                if len(records) > MAX_IMPORT_QUESTIONS:
                    raise ImportFormatError(f"An import may contain no more than {MAX_IMPORT_QUESTIONS} questions.")
            current = {"text": question_match.group(1), "choices": [], "correct": None, "marks": 1}
            continue
        if current is None:
            if line.casefold().startswith("question:"):
                current = {"text": line.split(":", 1)[1].strip(), "choices": [], "correct": None, "marks": 1}
            continue
        option_match = re.match(r"^([A-E])[\).]\s*(.+)$", line, re.I)
        if option_match:
            current["choices"].append(option_match.group(2).strip())
            continue
        answer_match = re.match(r"^(?:correct\s+)?answer\s*:\s*([A-E]|\d+)\b", line, re.I)
        if answer_match:
            answer = answer_match.group(1).upper()
            current["correct"] = ord(answer) - ord("A") + 1 if answer in "ABCDE" else int(answer)
            continue
        marks_match = re.match(r"^(?:marks?|points?)\s*:\s*(\d+)", line, re.I)
        if marks_match:
            current["marks"] = marks_match.group(1)
            continue
        category_match = re.match(r"^category\s*:\s*(.+)", line, re.I)
        if category_match:
            current["category"] = category_match.group(1).strip()
            continue
        if current["choices"]:
            current["choices"][-1] += f" {line}"
        else:
            current["text"] += f" {line}"
    if current:
        records.append(_record(**current))
    if len(records) > MAX_IMPORT_QUESTIONS:
        raise ImportFormatError(f"An import may contain no more than {MAX_IMPORT_QUESTIONS} questions.")
    if not records:
        raise ImportFormatError(
            "No questions found. Use numbered questions followed by A) options and an Answer: A line."
        )
    for record in records:
        normalized_choices = [choice.casefold() for choice in record["choices"]]
        if normalized_choices in (["true", "false"], ["false", "true"], ["yes", "no"], ["no", "yes"]):
            record["question_type"] = Question.Type.TRUE_FALSE
    return records


def _moodle_xml(raw):
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError) as exc:
        raise ImportFormatError("The Moodle XML file is not well-formed.") from exc
    plain = _PlainText()
    records = []
    category = ""
    for question in root.findall(".//question"):
        kind = question.get("type", "")
        if kind == "category":
            category_path = plain.text(question.findtext("category/text", default=""))
            category = category_path.rsplit("/", 1)[-1]
            continue
        name = question.findtext("name/text", default="")
        text = plain.text(question.findtext("questiontext/text", default=""))
        label = name.strip() or text[:80]
        if kind == "truefalse":
            answers = question.findall("answer")
            options = [plain.text(answer.findtext("text", default="")) for answer in answers]
            correct_indexes = [i for i, answer in enumerate(answers, 1) if _is_full_credit(answer.get("fraction"))]
            record = _record(
                text, options, correct_indexes[0] if len(correct_indexes) == 1 else 0,
                _moodle_marks(question), category, Question.Type.TRUE_FALSE,
            )
        elif kind == "multichoice":
            answers = question.findall("answer")
            options = [plain.text(answer.findtext("text", default="")) for answer in answers]
            correct_indexes = [i for i, answer in enumerate(answers, 1) if _is_full_credit(answer.get("fraction"))]
            record = _record(
                text, options, correct_indexes[0] if len(correct_indexes) == 1 else 0,
                _moodle_marks(question), category,
            )
        else:
            record = _record(text, [], None, _moodle_marks(question), category, kind or "unknown")
        if kind in {"multichoice", "truefalse"} and len(correct_indexes) > 1:
            record["errors"].append("Multiple correct answers are not supported by this assessment.")
        if record["errors"]:
            record["errors"].insert(0, f"{label}:")
        records.append(record)
        if len(records) > MAX_IMPORT_QUESTIONS:
            raise ImportFormatError(f"An import may contain no more than {MAX_IMPORT_QUESTIONS} questions.")
    if not records:
        raise ImportFormatError("No supported questions were found in the Moodle XML file.")
    return records


def _is_full_credit(fraction):
    try:
        return float(fraction) == 100
    except (TypeError, ValueError):
        return False


def _moodle_marks(question):
    raw_marks = question.get("defaultgrade", "1")
    try:
        value = float(raw_marks)
    except (TypeError, ValueError):
        return 0
    return int(value) if value.is_integer() else 0


def _validate_archive(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 5000 or sum(member.file_size for member in members) > 50 * 1024 * 1024:
                raise ImportFormatError("The document expands beyond the allowed size.")
    except zipfile.BadZipFile as exc:
        raise ImportFormatError("The document archive is invalid.") from exc


def parse_upload(upload):
    """Validate and parse one upload into editable, normalized preview rows."""
    suffix = Path(upload.name).suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ImportFormatError("Supported file types are .xlsx, .xls, .csv, .docx, .pdf, and Moodle .xml.")
    if upload.size == 0 or upload.size > MAX_IMPORT_BYTES:
        raise ImportFormatError("The file must be non-empty and no larger than 10 MB.")
    raw = upload.read()
    upload.seek(0)
    try:
        if suffix == ".csv":
            try:
                rows = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
            except UnicodeDecodeError as exc:
                raise ImportFormatError("CSV files must use UTF-8 encoding.") from exc
            records = _from_table(rows)
        elif suffix == ".xlsx":
            if not zipfile.is_zipfile(io.BytesIO(raw)):
                raise ImportFormatError("The XLSX file is invalid.")
            _validate_archive(raw)
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException

            try:
                workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            except (ValueError, KeyError, IndexError, InvalidFileException, zipfile.BadZipFile) as exc:
                raise ImportFormatError("The XLSX file is invalid.") from exc
            try:
                records = _from_table(workbook.active.iter_rows(values_only=True))
            finally:
                workbook.close()
        elif suffix == ".xls":
            import xlrd

            try:
                workbook = xlrd.open_workbook(file_contents=raw, on_demand=True)
            except xlrd.XLRDError as exc:
                raise ImportFormatError("The XLS file is invalid.") from exc
            try:
                sheet = workbook.sheet_by_index(0)
                records = _from_table(sheet.row_values(i) for i in range(sheet.nrows))
            except IndexError as exc:
                raise ImportFormatError("The XLS file contains no worksheets.") from exc
            finally:
                workbook.release_resources()
        elif suffix == ".docx":
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
            from lxml.etree import XMLSyntaxError

            _validate_archive(raw)
            try:
                document = Document(io.BytesIO(raw))
            except (ValueError, KeyError, XMLSyntaxError, zipfile.BadZipFile, PackageNotFoundError) as exc:
                raise ImportFormatError("The Word document is invalid.") from exc
            paragraphs = [paragraph.text for paragraph in document.paragraphs]
            paragraphs.extend(
                cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            )
            records = _text_questions("\n".join(paragraphs))
        elif suffix == ".pdf":
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            if not raw.startswith(b"%PDF-"):
                raise ImportFormatError("The PDF file is invalid.")
            try:
                reader = PdfReader(io.BytesIO(raw), strict=True)
                if reader.is_encrypted:
                    raise ImportFormatError("Encrypted PDF files are not supported.")
                if len(reader.pages) > 100:
                    raise ImportFormatError("PDF imports are limited to 100 pages.")
                records = _text_questions("\n".join(page.extract_text() or "" for page in reader.pages))
            except (PdfReadError, ValueError, OSError) as exc:
                raise ImportFormatError("The PDF file could not be read.") from exc
        else:
            records = _moodle_xml(raw)
    except (csv.Error, zipfile.BadZipFile) as exc:
        raise ImportFormatError("The uploaded file could not be read.") from exc
    if len(records) > MAX_IMPORT_QUESTIONS:
        raise ImportFormatError(f"An import may contain no more than {MAX_IMPORT_QUESTIONS} questions.")
    return records
