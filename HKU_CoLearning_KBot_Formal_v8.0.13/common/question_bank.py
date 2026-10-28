"""Bounded Excel parsing for teacher-authored TF and single-answer MCQ."""
import io
import zipfile
from openpyxl import load_workbook
from common.question_bank_extended import QUESTION_HEADERS, parse_extended

HEADERS = ["question_id", "question_type", "topic", "question", "option_A", "option_B", "option_C", "option_D", "option_E", "correct_answer", "explanation"]
MAX_FILE_BYTES = 5 * 1024 * 1024


def parse_workbook(content):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Maximum file size is 5 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 20 * 1024 * 1024:
                raise ValueError("Expanded workbook exceeds 20 MB.")
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid Excel workbook. Please use the .xlsx template.") from exc
    try:
        sheet = book["Questions"] if "Questions" in book.sheetnames else book.active
        # Iterate actual cells as well: producer dimension metadata is not reliable.
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        first = next(rows, ())
        header = [str(c.value or "").strip() for c in first]
        if header == QUESTION_HEADERS:
            return parse_extended(book)
        if header != HEADERS:
            raise ValueError("Column headers must match the downloaded template exactly.")
        questions, errors, seen = [], [], set()
        for number, cells in enumerate(rows, 2):
            if number > 1001:
                raise ValueError("Maximum 1000 question rows per upload.")
            if len(cells) > len(HEADERS):
                raise ValueError("Unexpected columns. Use the downloaded template.")
            if not any(c.value is not None for c in cells):
                continue
            values = [str(c.value).strip() if c.value is not None else "" for c in cells]
            values += [""] * (len(HEADERS) - len(values))
            row = dict(zip(HEADERS, values))
            issues = []
            if any(c.data_type == "f" for c in cells):
                issues.append("Formulas are not accepted; paste values instead.")
            for field, limit in [("question_id", 80), ("topic", 256), ("question", 10000), ("explanation", 10000)]:
                if len(row[field]) > limit:
                    issues.append(f"{field} exceeds {limit} characters.")
            if any(len(row["option_" + k]) > 10000 for k in "ABCDE"):
                issues.append("An option exceeds 10000 characters.")
            if not row["question_id"] or not row["question"]:
                issues.append("question_id and question are required.")
            key = row["question_id"].casefold()
            if key in seen:
                issues.append("Duplicate question_id in this upload (case-insensitive).")
            seen.add(key)
            kind = row["question_type"].upper()
            answer = row["correct_answer"].upper()
            options = {k: row["option_" + k] for k in "ABCDE" if row["option_" + k]}
            if kind == "TF":
                if answer not in ("TRUE", "FALSE"):
                    issues.append("TF answer must be TRUE or FALSE.")
                if options:
                    issues.append("Leave option columns empty for TF.")
            elif kind == "MCQ":
                if len(options) < 2:
                    issues.append("MCQ requires at least two options.")
                if answer not in options:
                    issues.append("MCQ answer must be one existing option letter A–E.")
                if len({v.casefold() for v in options.values()}) != len(options):
                    issues.append("Duplicate option text is not accepted.")
            else:
                issues.append("question_type must be TF or MCQ.")
            if issues:
                errors.append({"row": number, "errors": issues})
            else:
                questions.append(dict(row=number, question_id=row["question_id"], question_type=kind, topic=row["topic"], question=row["question"], options=options, correct_answer=answer, explanation=row["explanation"]))
        if not questions and not errors:
            raise ValueError("The workbook contains no questions.")
        return questions, errors
    finally:
        book.close()
