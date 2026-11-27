"""Shared MCQ answer contract for imports, generation, grading and review."""
from __future__ import annotations

import re
from typing import Any


def answer_letters(value: object, available: object = "ABCDE") -> list[str]:
    """Accept B or A,C (also compact AC); reject duplicates and missing options.

    Storage uses compact letters (at most five characters), so existing MySQL
    correct_answer VARCHAR(8) columns do not need a migration.
    """
    raw = str(value or "").strip().upper()
    if not raw or not re.fullmatch(r"[A-E\s,;|]+", raw):
        raise ValueError("MCQ answer must use existing option letters A–E, e.g. B or A,C.")
    letters = re.findall(r"[A-E]", raw)
    if not letters or len(letters) != len(set(letters)):
        raise ValueError("MCQ correct answers must not contain duplicate letters.")
    if not set(letters) <= set(available):
        raise ValueError("MCQ answer must reference existing option letters only.")
    return sorted(letters)


def correct_indices(quiz: dict[str, Any]) -> list[int]:
    values = quiz.get("correct_indices") if "correct_indices" in quiz else [quiz.get("correct_index")]
    if (not isinstance(values, list) or not values
            or not all(type(value) is int and 0 <= value < len(quiz.get("options", [])) for value in values)
            or len(values) != len(set(values))):
        raise ValueError("MCQ needs distinct, in-range correct option indexes.")
    return sorted(values)


def selected_indices(answer: object, options: list[str]) -> list[int] | None:
    """Browser arrays are zero-based indexes. Letter arrays are also accepted."""
    if not isinstance(answer, list) or not answer:
        return None
    values = []
    for item in answer:
        if type(item) is int:
            value = item
        elif isinstance(item, str) and re.fullmatch(r"[A-Za-z]", item.strip()):
            value = ord(item.strip().upper()) - ord("A")
        elif isinstance(item, str) and re.fullmatch(r"\d+", item.strip()):
            value = int(item)
        else:
            return None
        if not 0 <= value < len(options):
            return None
        values.append(value)
    return sorted(values) if len(values) == len(set(values)) else None
