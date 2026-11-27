from __future__ import annotations

import json
import re
from typing import Any

from .mcq_answers import correct_indices, selected_indices


_TYPE_LABELS = {
    "true_false": "True/False",
    "mcq": "MCQ",
    "matching": "Matching",
    "fill_blank": "Fill Blank",
    "short_answer": "Short Answer",
}


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value
    else:
        raw = str(value)
    return re.sub(r"\s+", " ", raw).strip()


def _jsonish(value: object) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return _text(value)


def _normalise(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).casefold()).strip()


def _quiz_type(value: object) -> str:
    raw = _text(value)
    aliases = {
        "true false": "true_false",
        "truefalse": "true_false",
        "tf": "true_false",
        "mcq": "mcq",
        "multiple choice": "mcq",
        "multiplechoice": "mcq",
        "matching": "matching",
        "match": "matching",
        "fill blank": "fill_blank",
        "fill in the blank": "fill_blank",
        "fillblank": "fill_blank",
        "short answer": "short_answer",
        "shortanswer": "short_answer",
    }
    return aliases.get(_normalise(raw), raw)


def _selected_index(value: object, options: list[str]) -> int | None:
    raw = _text(value)
    if len(raw) == 1 and raw.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        index = ord(raw.upper()) - ord("A")
        return index if 0 <= index < len(options) else None
    if raw.casefold() in {"true", "false"} and options == ["True", "False"]:
        return 0 if raw.casefold() == "true" else 1
    try:
        number = int(float(raw))
        if 0 <= number < len(options):
            return number
        if 1 <= number <= len(options):
            return number - 1
    except (TypeError, ValueError):
        pass
    normalised = _normalise(raw)
    for index, option in enumerate(options):
        if _normalise(option) == normalised:
            return index
    return None


def _student_answer_text(quiz: dict[str, Any], student_answer: object) -> str:
    quiz_type = _quiz_type(quiz.get("type"))
    if quiz_type in {"true_false", "mcq"}:
        options = [_text(item) for item in quiz.get("options", [])]
        if quiz_type == "mcq" and isinstance(student_answer, list):
            selected = selected_indices(student_answer, options)
            return "; ".join(f"{chr(65 + index)}. {options[index]}" for index in (selected or [])) or "(no valid selection)"
        selected = _selected_index(student_answer, options)
        if selected is not None and 0 <= selected < len(options):
            return f"{chr(65 + selected)}. {options[selected]}"
    return _jsonish(student_answer)



def _decode_json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return value


def build_review_payload(
    quiz: dict[str, Any],
    student_answer: object,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return the student-safe post-submission review shown in Quiz and History.

    This deliberately omits generation/grading internals, citations, excerpts and
    service metadata.  The complete teacher-facing audit text remains stored in
    ``ai_feedback`` and is not changed by this view model.
    """
    quiz_type = _quiz_type(quiz.get("type"))
    question = _text(quiz.get("question"))
    review: dict[str, Any] = {
        "question_type": _TYPE_LABELS.get(quiz_type, quiz_type or "Unknown"),
        "question": question,
        "student_answer": _student_answer_text(quiz, student_answer),
        "result": "Correct" if bool(result.get("is_correct")) else "Incorrect",
        "score": int(result.get("score") or 0),
        "options": [],
        "correct_label": "Correct answer",
        "correct_answer": _text(result.get("correct_answer")),
        "submitted_pairs": [],
        "correct_pairs": [],
    }

    if quiz_type in {"true_false", "mcq"}:
        options = [_text(item) for item in quiz.get("options", [])]
        try:
            expected = correct_indices(quiz)
        except ValueError:
            expected = []
        chosen = (selected_indices(student_answer, options) or []) if isinstance(student_answer, list) else [_selected_index(student_answer, options)]
        review["options"] = [
            {"label": chr(65 + index), "text": option,
             "is_correct": index in expected, "is_selected": index in chosen}
            for index, option in enumerate(options)
        ]
        if expected:
            review["correct_label"] = "Correct options" if len(expected) > 1 else "Correct option"
            review["correct_answer"] = "; ".join(f"{chr(65 + index)}. {options[index]}" for index in expected)

    elif quiz_type == "matching":
        left = [_text(item) for item in quiz.get("left", [])]
        right = [_text(item) for item in quiz.get("right", [])]
        mapping = quiz.get("correct_mapping", []) if isinstance(quiz.get("correct_mapping"), list) else []
        submitted = student_answer if isinstance(student_answer, list) else []
        submitted_pairs: list[str] = []
        correct_pairs: list[str] = []
        for index, term in enumerate(left):
            try:
                selected = int(submitted[index])
            except (IndexError, TypeError, ValueError):
                selected = -1
            selected_text = right[selected] if 0 <= selected < len(right) else "(no valid selection)"
            submitted_pairs.append(f"{term} -> {selected_text}")
            try:
                target = int(mapping[index])
            except (IndexError, TypeError, ValueError):
                target = -1
            target_text = right[target] if 0 <= target < len(right) else "(missing mapping)"
            correct_pairs.append(f"{term} -> {target_text}")
        review["submitted_pairs"] = submitted_pairs
        review["correct_pairs"] = correct_pairs
        review["student_answer"] = "; ".join(submitted_pairs)
        review["correct_label"] = "Correct pairs"
        review["correct_answer"] = "; ".join(correct_pairs)

    elif quiz_type == "fill_blank":
        review["correct_answer"] = _text(quiz.get("answer") or result.get("correct_answer"))

    elif quiz_type == "short_answer":
        review["correct_label"] = "Reference answer"
        review["correct_answer"] = _text(quiz.get("reference") or result.get("correct_answer"))

    return review


def build_record_review(record: object) -> dict[str, Any]:
    """Reconstruct the same student-safe review from a persisted attempt."""
    decoded = _decode_json_value(getattr(record, "options_or_pairs", ""))
    payload = decoded if isinstance(decoded, dict) else {}
    quiz_type = _quiz_type(getattr(record, "question_type", ""))
    quiz: dict[str, Any] = {
        "type": quiz_type,
        "question": getattr(record, "question", ""),
    }
    for key in ("options", "correct_index", "correct_indices", "left", "right", "correct_mapping", "answer", "reference", "key_points"):
        if key in payload:
            quiz[key] = payload[key]

    student_answer = _decode_json_value(getattr(record, "answer", ""))
    result = {
        "is_correct": bool(getattr(record, "is_correct", False)),
        "score": int(getattr(record, "score", 0) or 0),
        "correct_answer": _decode_json_value(getattr(record, "correct_answer", "")),
    }
    review = build_review_payload(quiz, student_answer, result)
    review["teacher_feedback"] = _text(getattr(record, "teacher_feedback", ""))
    saved = payload.get("review_feedback")
    saved = saved if isinstance(saved, dict) else {}
    feedback = _text(saved.get("reasoning"))
    if not feedback:
        # Recover only the documented student-facing line, never raw audit text.
        for line in str(getattr(record, "ai_feedback", "") or "").splitlines():
            if line.startswith("AI grading comment: "):
                feedback = line.partition(": ")[2]
                break
    review["feedback"] = feedback
    review["explanation"] = _text(getattr(record, "explanation", ""))
    review["pair_feedback"] = saved.get("pair_feedback", [])
    review["key_point_results"] = saved.get("key_point_results", [])
    references = _decode_json_value(getattr(record, "citations_json", ""))
    review["citations"] = [item for item in references if isinstance(item, dict)] if isinstance(references, list) else []
    source = _text(getattr(record, "generation_source", "") or getattr(record, "source", ""))
    review["source_label"] = ("From question bank" if source in {"teacher_upload", "question_bank"}
                               else "AI 生成 · Generated by AI" if source else "Source not recorded")
    return review


def _reference_line(item: dict[str, Any], index: int) -> str:
    source = _text(item.get("source_file")) or "Unknown source"
    section = _text(item.get("section") or item.get("chapter"))
    page = item.get("page")
    location = source
    if section:
        location += f" | section: {section}"
    if page is not None and str(page).strip():
        location += f" | page: {page}"
    excerpt = _text(item.get("excerpt"))
    if len(excerpt) > 500:
        excerpt = excerpt[:497].rstrip() + "..."
    return f"{index}. {location}" + (f" | excerpt: {excerpt}" if excerpt else "")


def build_detailed_feedback(
    quiz: dict[str, Any],
    student_answer: object,
    result: dict[str, Any],
    citations: list[dict[str, Any]] | None,
) -> str:
    """Build a complete post-submission record without changing grading behaviour.

    The text deliberately duplicates the structured question fields.  This gives the
    Teacher App and legacy exports a readable recovery source if an older migration
    omitted ``options_or_pairs`` or ``correct_answer``.
    """
    quiz_type = _quiz_type(quiz.get("type"))
    question = _text(quiz.get("question"))
    score = int(result.get("score") or 0)
    is_correct = bool(result.get("is_correct"))
    reasoning = _text(result.get("reasoning"))
    explanation = _text(quiz.get("explanation"))

    lines = [
        "[KBot Detailed Feedback v1]",
        f"Question type: {_TYPE_LABELS.get(quiz_type, quiz_type or 'Unknown')}",
        f"Question: {question}",
        f"Student answer: {_student_answer_text(quiz, student_answer)}",
        f"Result: {'Correct' if is_correct else 'Incorrect'}",
        f"Score: {score}/100",
    ]

    if quiz_type in {"true_false", "mcq"}:
        options = [_text(item) for item in quiz.get("options", [])]
        review = build_review_payload(quiz, student_answer, result)
        lines.append("Options:")
        for option in review["options"]:
            tags = []
            if option["is_correct"]:
                tags.append("CORRECT")
            if option["is_selected"]:
                tags.append("STUDENT SELECTED")
            suffix = f" [{'; '.join(tags)}]" if tags else ""
            lines.append(f"{option['label']}. {option['text']}{suffix}")
        lines.append(f"{review['correct_label']}: {review['correct_answer']}")
        other_options = [f"{option['label']}. {option['text']}" for option in review["options"] if not option["is_correct"]]
        if other_options:
            lines.append("Other options: " + " | ".join(other_options))

    elif quiz_type == "matching":
        left = [_text(item) for item in quiz.get("left", [])]
        right = [_text(item) for item in quiz.get("right", [])]
        mapping = quiz.get("correct_mapping", []) if isinstance(quiz.get("correct_mapping"), list) else []
        submitted = student_answer if isinstance(student_answer, list) else []
        lines.append("Submitted pairs:")
        for index, term in enumerate(left):
            try:
                selected = int(submitted[index])
            except (IndexError, TypeError, ValueError):
                selected = -1
            definition = right[selected] if 0 <= selected < len(right) else "(no valid selection)"
            lines.append(f"{index + 1}. {term} -> {definition}")
        lines.append("Correct pairs:")
        for index, term in enumerate(left):
            try:
                target = int(mapping[index])
            except (IndexError, TypeError, ValueError):
                target = -1
            definition = right[target] if 0 <= target < len(right) else "(missing mapping)"
            lines.append(f"{index + 1}. {term} -> {definition}")

    elif quiz_type == "fill_blank":
        lines.append(f"Correct answer: {_text(quiz.get('answer') or result.get('correct_answer'))}")

    elif quiz_type == "short_answer":
        key_points = [_text(item) for item in quiz.get("key_points", []) if _text(item)]
        if key_points:
            lines.append("Required key points:")
            lines.extend(f"{index}. {point}" for index, point in enumerate(key_points, 1))
        point_results = result.get("key_point_results") if isinstance(result.get("key_point_results"), list) else []
        if point_results:
            lines.append("Key-point assessment:")
            for item in point_results:
                lines.append(
                    f"{item.get('index')}. {int(item.get('credit') or 0)}/100 | {_text(item.get('comment'))}"
                )
        lines.append(f"Reference answer: {_text(quiz.get('reference') or result.get('correct_answer'))}")

    else:
        correct_answer = result.get("correct_answer")
        if correct_answer is not None:
            lines.append(f"Correct answer: {_jsonish(correct_answer)}")

    if reasoning:
        lines.append(f"AI grading comment: {reasoning}")
    if explanation:
        lines.append(f"Explanation: {explanation}")

    references = [item for item in (citations or []) if isinstance(item, dict)]
    lines.append("Course references:")
    if references:
        lines.extend(_reference_line(item, index) for index, item in enumerate(references, 1))
    else:
        lines.append("None recorded.")

    generation_source = _text(result.get("generation_source"))
    grading_source = _text(result.get("grading_source"))
    if generation_source:
        lines.append(f"Generation source: {generation_source}")
    if grading_source:
        lines.append(f"Grading source: {grading_source}")
    return "\n".join(lines)
