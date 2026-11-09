"""Structured Matching, Fill Blank and Short Answer workbook contracts."""
import re
from decimal import Decimal, InvalidOperation

QUESTION_HEADERS = ["question_id", "question_type", "topic", "question", "reference_answer", "explanation"]
DETAIL_HEADERS = {
    "Matching": ["question_id", "left_id", "left_text", "right_id", "right_text"],
    "Blanks": ["question_id", "blank_id", "accepted_answer", "ignore_case"],
    "Rubric": ["question_id", "criterion_id", "criterion", "points"],
}
SHEET_FOR_TYPE = {"MATCHING": "Matching", "FILL": "Blanks", "SHORT": "Rubric"}
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
BLANK_PATTERN = re.compile(r"\{\{(blank[1-9][0-9]*)\}\}")


def _sheet_rows(book, name, headers, limit):
    if name not in book.sheetnames:
        return [], []
    sheet = book[name]
    sheet.reset_dimensions()
    cells = sheet.iter_rows()
    if [str(c.value or "").strip() for c in next(cells, ())] != headers:
        raise ValueError(f"Sheet {name}: column headers must match its template exactly.")
    result, errors = [], []
    for number, row in enumerate(cells, 2):
        if number > limit + 1:
            raise ValueError(f"Sheet {name}: maximum {limit} data rows.")
        if len(row) > len(headers):
            raise ValueError(f"Sheet {name}: unexpected columns.")
        if not any(cell.value is not None for cell in row):
            continue
        values = [str(c.value).strip() if c.value is not None else "" for c in row]
        values.extend([""] * (len(headers) - len(values)))
        item = dict(zip(headers, values))
        problems = []
        if any(c.data_type == "f" for c in row):
            problems.append("Formulas are not accepted; paste values instead.")
        if any(len(v) > 10000 for v in values):
            problems.append("A cell exceeds 10000 characters.")
        if not item["question_id"] or len(item["question_id"]) > 80:
            problems.append("question_id is required and must not exceed 80 characters.")
        if problems:
            errors.append(dict(sheet=name, row=number, errors=problems))
        # Keep row references even on error so no offending detail can silently disappear.
        result.append((number, item))
    return result, errors


def _matching(rows):
    errors, pairs = [], []
    used_left, used_right, left_texts, right_texts = set(), set(), set(), set()
    if not 2 <= len(rows) <= 20:
        errors.append((None, "Matching requires 2–20 one-to-one pairs."))
    for number, row in rows:
        if any(not row[key] for key in ("left_id", "left_text", "right_id", "right_text")):
            errors.append((number, "Each pair requires both IDs and both texts."))
        for key, seen in (("left_id", used_left), ("right_id", used_right)):
            value = row[key]
            if not ID_PATTERN.fullmatch(value):
                errors.append((number, f"{key} must start with a letter and use at most 40 letters, digits or underscores."))
            if value.casefold() in seen:
                errors.append((number, f"Duplicate {key}; only one-to-one matching is supported."))
            seen.add(value.casefold())
        for key, seen in (("left_text", left_texts), ("right_text", right_texts)):
            value = row[key].casefold()
            if value in seen:
                errors.append((number, f"Duplicate {key} creates ambiguous pairs."))
            seen.add(value)
        pairs.append({key: row[key] for key in ("left_id", "left_text", "right_id", "right_text")})
    return dict(schema_version=1, pairs=pairs, correct_pairs={p["left_id"]: p["right_id"] for p in pairs}), errors


def _blanks(question, rows):
    errors, blanks = [], {}
    placeholders = BLANK_PATTERN.findall(question)
    if not placeholders or len(placeholders) > 20 or len(set(placeholders)) != len(placeholders):
        errors.append((None, "Use 1–20 unique placeholders {{blank1}}, {{blank2}}, etc.; each appears once."))
    remainder = BLANK_PATTERN.sub("", question)
    if "{{" in remainder or "}}" in remainder:
        errors.append((None, "Invalid placeholder; use {{blank1}}, {{blank2}}, etc."))
    for number, row in rows:
        blank_id = row["blank_id"]
        if blank_id not in placeholders:
            errors.append((number, f"blank_id {blank_id!r} has no matching placeholder in the question."))
        flag = row["ignore_case"].upper()
        if flag not in ("TRUE", "FALSE"):
            errors.append((number, "ignore_case must be TRUE or FALSE."))
        answer = row["accepted_answer"]
        if not answer:
            errors.append((number, "accepted_answer is required."))
        blank = blanks.setdefault(blank_id, dict(id=blank_id, accepted_answers=[], ignore_case=flag == "TRUE", trim_whitespace=True))
        if blank["ignore_case"] != (flag == "TRUE"):
            errors.append((number, "All answers for the same blank must use the same ignore_case setting."))
        normalized = answer.casefold() if blank["ignore_case"] else answer
        if normalized in [a.casefold() if blank["ignore_case"] else a for a in blank["accepted_answers"]]:
            errors.append((number, "Duplicate accepted answer for this blank."))
        if len(blank["accepted_answers"]) >= 20:
            errors.append((number, "Maximum 20 accepted answers per blank."))
        blank["accepted_answers"].append(answer)
    for blank_id in placeholders:
        if blank_id not in blanks:
            errors.append((None, f"Missing accepted answer for {blank_id}."))
    return dict(schema_version=1, blanks=[blanks[k] for k in placeholders if k in blanks]), errors


def _rubric(reference, rows):
    errors, criteria, seen, total = [], [], set(), Decimal(0)
    if not reference:
        errors.append((None, "Short Answer requires reference_answer in Questions."))
    if not 1 <= len(rows) <= 20:
        errors.append((None, "Short Answer requires 1–20 rubric criteria."))
    for number, row in rows:
        identifier = row["criterion_id"]
        if not ID_PATTERN.fullmatch(identifier) or identifier.casefold() in seen:
            errors.append((number, "criterion_id must be unique and use 1–40 letters, digits or underscores, starting with a letter."))
        seen.add(identifier.casefold())
        if not row["criterion"]:
            errors.append((number, "criterion is required."))
        try:
            # Avoid evaluating huge or non-finite decimal values from a workbook.
            raw = row["points"]
            if not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,2})?", raw):
                raise InvalidOperation
            points = Decimal(raw)
            if not 0 < points <= 1000:
                raise InvalidOperation
        except InvalidOperation:
            errors.append((number, "points must be positive, at most 1000, with up to two decimal places."))
            continue
        total += points
        criteria.append(dict(id=identifier, criterion=row["criterion"], points=str(points)))
    if total > 1000:
        errors.append((None, "Total rubric score must not exceed 1000."))
    return dict(schema_version=1, reference_answer=reference, rubric=criteria, max_score=str(total)), errors


def parse_extended(book):
    if "Questions" not in book.sheetnames:
        raise ValueError("Structured workbooks require a Questions sheet.")
    unexpected = set(book.sheetnames) - {"Questions", *DETAIL_HEADERS}
    if unexpected:
        raise ValueError("Unexpected sheets: " + ", ".join(sorted(unexpected)))
    main, errors = _sheet_rows(book, "Questions", QUESTION_HEADERS, 1000)
    if not main:
        raise ValueError("The workbook contains no questions.")
    question_map, grouped, bad_main = {}, {}, set()
    for number, row in main:
        key = row["question_id"].casefold()
        issues = []
        if key in question_map:
            issues.append("Duplicate question_id in Questions (case-insensitive).")
        else:
            question_map[key] = (number, row)
        if row["question_type"].upper() not in SHEET_FOR_TYPE:
            issues.append("Structured question_type must be MATCHING, FILL or SHORT.")
        if not row["question"]:
            issues.append("question is required.")
        if len(row["topic"]) > 256:
            issues.append("topic exceeds 256 characters.")
        if row["question_type"].upper() != "SHORT" and row["reference_answer"]:
            issues.append("reference_answer is only used for SHORT; leave it empty for Matching and Fill Blank.")
        if issues:
            bad_main.add(number)
            errors.append(dict(sheet="Questions", row=number, errors=issues))
    for sheet, headers in DETAIL_HEADERS.items():
        detail_rows, sheet_errors = _sheet_rows(book, sheet, headers, 10000)
        errors.extend(sheet_errors)
        for number, row in detail_rows:
            key = row["question_id"].casefold()
            parent = question_map.get(key)
            if not parent or SHEET_FOR_TYPE.get(parent[1]["question_type"].upper()) != sheet:
                errors.append(dict(sheet=sheet, row=number, errors=["question_id must refer to a Questions row of the corresponding type."]))
            else:
                grouped.setdefault(key, []).append((number, row))
    questions = []
    for number, row in main:
        key, kind = row["question_id"].casefold(), row["question_type"].upper()
        if number in bad_main or kind not in SHEET_FOR_TYPE:
            continue
        if SHEET_FOR_TYPE[kind] not in book.sheetnames:
            errors.append(dict(sheet="Questions", row=number, errors=[f"Missing {SHEET_FOR_TYPE[kind]} sheet."]))
            continue
        details = grouped.get(key, [])
        if kind == "MATCHING":
            payload, problems = _matching(details)
        elif kind == "FILL":
            payload, problems = _blanks(row["question"], details)
        else:
            payload, problems = _rubric(row["reference_answer"], details)
        for detail_number, message in problems:
            errors.append(dict(sheet=SHEET_FOR_TYPE[kind] if detail_number else "Questions", row=detail_number or number, errors=[message]))
        if not problems:
            questions.append(dict(row=number, question_id=row["question_id"], question_type=kind, topic=row["topic"], question=row["question"], options={}, correct_answer="", explanation=row["explanation"], details=payload))
    return questions, errors
