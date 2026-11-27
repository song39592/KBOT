from __future__ import annotations

import json
import random
import re
from typing import Any

from .json_recovery import StructuredOutputError, coerce_bool, parse_json_like
from .mcq_answers import correct_indices


class QuizValidationError(ValueError):
    pass


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_type(value: object) -> str:
    raw = clean_text(value).casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "tf": "true_false", "truefalse": "true_false", "true_or_false": "true_false", "true/false": "true_false",
        "multiple_choice": "mcq", "multiplechoice": "mcq", "mcq_two": "mcq", "mcq_four": "mcq",
        "fill": "fill_blank", "fillblank": "fill_blank", "fill_in_the_blank": "fill_blank",
        "short": "short_answer", "shortanswer": "short_answer",
    }
    return aliases.get(raw, "mcq" if raw.startswith("mcq") else raw)


def _first(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    lowered = {str(key).casefold(): value for key, value in payload.items()}
    for key in keys:
        if key in payload:
            return payload[key]
        if key.casefold() in lowered:
            return lowered[key.casefold()]
    return default


def _list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str) and value.strip():
        try:
            parsed = parse_json_like(value)
            if isinstance(parsed, list):
                return parsed
        except StructuredOutputError:
            pass
        return [part.strip() for part in re.split(r"\n|;|\|", value) if part.strip()]
    return []


def _index(value: Any, options: list[str], *, explicitly_zero_based: bool = False) -> int | None:
    if isinstance(value, bool):
        return int(value)
    raw = clean_text(value)
    if not raw:
        return None
    if len(raw) == 1 and raw.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        result = ord(raw.upper()) - ord("A")
        return result if 0 <= result < len(options) else None
    try:
        number = int(float(raw))
        if explicitly_zero_based and 0 <= number < len(options):
            return number
        if number == 0:
            return 0
        if 1 <= number <= len(options):
            return number - 1
    except ValueError:
        pass
    normalized = raw.casefold()
    return next((index for index, option in enumerate(options) if clean_text(option).casefold() == normalized), None)


def _pair(item: Any) -> tuple[str, str] | None:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        left, right = clean_text(item[0]), clean_text(item[1])
        return (left, right) if left and right else None
    if not isinstance(item, dict):
        return None
    left = _first(item, "term", "left", "l", "concept", "key", "item", "a")
    right = _first(item, "definition", "right", "r", "meaning", "value", "answer", "match", "b")
    left, right = clean_text(left), clean_text(right)
    return (left, right) if left and right else None


def salvage_pairs(raw: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    patterns = [
        r'"(?:term|left|l|concept)"\s*:\s*"([^"]+)"\s*,\s*"(?:definition|right|r|meaning)"\s*:\s*"([^"]+)"',
        r'\[\s*"([^"]{2,80})"\s*,\s*"([^"]{5,400})"(?:\s*,\s*"[^"]*")?\s*\]',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, str(raw or ""), re.IGNORECASE):
            pair = (clean_text(match.group(1)), clean_text(match.group(2)))
            if all(pair) and pair not in output:
                output.append(pair)
    return output


def matching_pairs(payload: dict[str, Any], raw: str = "") -> list[tuple[str, str]]:
    pair_value = _first(payload, "p", "pairs", "matching_pairs", "matches")
    if isinstance(pair_value, dict):
        direct = [(clean_text(left), clean_text(right)) for left, right in pair_value.items() if clean_text(left) and clean_text(right)]
        if direct:
            return direct
    direct = [pair for item in _list(pair_value) if (pair := _pair(item))]
    if direct:
        return direct

    left = [clean_text(item) for item in _list(_first(payload, "left", "l", "terms", "concepts")) if clean_text(item)]
    right = [clean_text(item) for item in _list(_first(payload, "right", "r", "definitions", "meanings")) if clean_text(item)]
    if left and right:
        mapping_value = _first(payload, "correct_mapping", "mapping", "answer", "correct", default=None)
        if isinstance(mapping_value, dict):
            mapping = [mapping_value.get(str(index), mapping_value.get(str(index + 1), mapping_value.get(term))) for index, term in enumerate(left)]
        else:
            mapping = _list(mapping_value)
        if mapping and len(mapping) >= len(left):
            numeric: list[int] = []
            for item in mapping[: len(left)]:
                try:
                    numeric.append(int(float(str(item))))
                except ValueError:
                    numeric = []
                    break
            one_based = bool(numeric) and 0 not in numeric and all(1 <= item <= len(right) for item in numeric)
            output: list[tuple[str, str]] = []
            for position, term in enumerate(left):
                item = mapping[position]
                if isinstance(item, dict):
                    item = _first(item, "right", "r", "index", "answer", "definition")
                mapped = int(float(str(item))) - 1 if one_based else _index(item, right, explicitly_zero_based=True)
                if mapped is not None and 0 <= mapped < len(right):
                    output.append((term, right[mapped]))
            if len(output) >= 2:
                return output
        if len(left) == len(right):
            return list(zip(left, right))
    return salvage_pairs(raw)


def normalize_quiz(raw: str | dict[str, Any] | list[Any], expected_type: str, shuffle_seed: str = "") -> dict[str, Any]:
    expected = canonical_type(expected_type)
    payload = parse_json_like(raw) if isinstance(raw, str) else raw
    if isinstance(payload, list) and expected == "matching":
        payload = {"pairs": payload}
    if not isinstance(payload, dict):
        raise QuizValidationError("Quiz output must be a JSON object.")
    nested = _first(payload, "quiz", "data")
    if isinstance(nested, dict) and (_first(nested, "question", "pairs", "p", "left") is not None):
        payload = nested
    actual = canonical_type(_first(payload, "type", "question_type", default=expected))
    if actual and actual != expected:
        raise QuizValidationError("Quiz type does not match the requested task.")
    question = clean_text(_first(payload, "question", "q", "prompt"))
    explanation = clean_text(_first(payload, "explanation", "reason", "feedback", "rationale"))

    if expected == "true_false":
        options = ["True", "False"]
        key_present = any(str(key).casefold() in {"correct_index", "answer_index"} for key in payload)
        if key_present:
            correct = _index(_first(payload, "correct_index", "answer_index"), options, explicitly_zero_based=True)
        else:
            answer = _first(payload, "correct_answer", "answer", "correct", "value", "is_true")
            parsed_bool = coerce_bool(answer, default=False)
            correct = 0 if parsed_bool else 1
        if not question or correct not in {0, 1}:
            raise QuizValidationError("True/False output is incomplete.")
        return {"type": expected, "question": question, "options": options, "correct_index": correct, "explanation": explanation}

    if expected == "mcq":
        options = [clean_text(item) for item in _list(_first(payload, "options", "choices", "answers", "items")) if clean_text(item)]
        if not 2 <= len(options) <= 5 or len({item.casefold() for item in options}) != len(options):
            raise QuizValidationError("MCQ must contain two to five distinct options.")
        if "correct_indices" in payload:
            try:
                indices = correct_indices({"options": options, "correct_indices": payload["correct_indices"]})
            except ValueError as exc:
                raise QuizValidationError(str(exc)) from exc
            if not question:
                raise QuizValidationError("MCQ output is incomplete.")
            return {"type": expected, "question": question, "options": options, "correct_indices": indices, "explanation": explanation}
        has_correct_index = any(str(key).casefold() == "correct_index" for key in payload)
        correct = _index(_first(payload, "correct_index", "answer_index", "correct", "correct_answer", "answer"), options, explicitly_zero_based=has_correct_index)
        if not question or correct is None:
            raise QuizValidationError("MCQ output is incomplete.")
        return {"type": expected, "question": question, "options": options, "correct_index": correct, "explanation": explanation}

    if expected == "matching":
        pairs = matching_pairs(payload, raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
        unique: list[tuple[str, str]] = []
        seen_terms: set[str] = set()
        seen_definitions: set[str] = set()
        for term, definition in pairs:
            if term.casefold() in seen_terms or definition.casefold() in seen_definitions:
                continue
            if len(term.split()) > 9 or len(term) > 90 or len(definition) < 5:
                continue
            unique.append((term, definition))
            seen_terms.add(term.casefold())
            seen_definitions.add(definition.casefold())
        if len(unique) < 2:
            raise QuizValidationError("Matching output contains fewer than two usable pairs.")
        unique = unique[:4]
        left = [item[0] for item in unique]
        original_right = [item[1] for item in unique]
        shuffled = list(enumerate(original_right))
        random.Random(shuffle_seed or "matching").shuffle(shuffled)
        right = [value for _, value in shuffled]
        positions = {old: new for new, (old, _) in enumerate(shuffled)}
        return {
            "type": expected, "question": question or "Match each term with its definition.",
            "left": left, "right": right, "correct_mapping": [positions[index] for index in range(len(left))],
            "explanation": explanation,
        }

    if expected == "fill_blank":
        answer = clean_text(_first(payload, "answer", "correct_answer", "blank", "value"))
        if not question or not answer:
            raise QuizValidationError("Fill Blank output is incomplete.")
        if "___" not in question and "[blank]" not in question.casefold():
            match = re.search(re.escape(answer), question, re.IGNORECASE)
            question = f"{question[:match.start()]}_____{question[match.end():]}" if match else f"{question} _____"
        return {"type": expected, "question": re.sub(r"\[blank\]", "_____", question, flags=re.IGNORECASE), "answer": answer, "explanation": explanation}

    if expected == "short_answer":
        raw_points = _first(payload, "key_points", "required_points", "marking_points", default=[])
        key_points = [clean_text(item) for item in _list(raw_points) if clean_text(item)]
        reference = _first(payload, "reference", "reference_answer", "correct_answer", "answer", "model_answer")
        if isinstance(reference, list):
            reference = "; ".join(clean_text(item) for item in reference if clean_text(item))
        reference = clean_text(reference)
        if not question or not reference:
            raise QuizValidationError("Short Answer output is incomplete.")
        return {
            "type": expected, "question": question, "reference": reference,
            "key_points": key_points, "explanation": explanation,
        }
    raise QuizValidationError(f"Unsupported quiz type: {expected}")

