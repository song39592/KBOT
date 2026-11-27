from __future__ import annotations

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from knowledge_service.retrieval import jaccard, tokens

from .coze_transport import AgentKind, CozeAgentRouter, CozeTransportError
from .json_recovery import clamp_int, coerce_bool, parse_json_like
from .quiz_parser import clean_text
from .mcq_answers import correct_indices, selected_indices


def normalize_answer(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).casefold()).strip()


def _selected_index(value: object, options: list[str]) -> int:
    raw = clean_text(str(value) if value is not None else "")
    if len(raw) == 1 and raw.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return ord(raw.upper()) - ord("A")
    if raw.casefold() in {"true", "false"} and options == ["True", "False"]:
        return 0 if raw.casefold() == "true" else 1
    try:
        number = int(float(raw))
        if 0 <= number < len(options):
            return number
        if 1 <= number <= len(options):
            return number - 1
    except ValueError:
        pass
    normalized = normalize_answer(raw)
    return next((index for index, option in enumerate(options) if normalize_answer(option) == normalized), -1)


def grade_objective(quiz: dict[str, Any], answer: Any) -> dict[str, Any]:
    quiz_type = quiz.get("type")
    if quiz_type in {"true_false", "mcq"}:
        options = quiz["options"]
        expected = correct_indices(quiz)
        if quiz_type == "mcq" and (len(expected) > 1 or isinstance(answer, list)):
            selected = selected_indices(answer, options)
        else:
            index = _selected_index(answer, options)
            selected = [index] if 0 <= index < len(options) else None
        correct = selected == expected
        chosen = "; ".join(options[index].rstrip(". ") for index in (selected or [])) or "no valid option"
        reference = "; ".join(options[index].rstrip(". ") for index in expected)
        reasoning = f"Correct. {reference} is the expected answer." if correct else f"You selected {chosen}. The correct answer is {reference}."
        missing = [options[index] for index in expected if index not in (selected or [])]
        extra = [options[index] for index in (selected or []) if index not in expected]
        if len(expected) > 1 and not correct:
            details = []
            if missing:
                details.append("Missing correct choices: " + "; ".join(missing))
            if extra:
                details.append("Choices that do not answer this question: " + "; ".join(extra))
            reasoning += " " + ". ".join(item.rstrip(". ") for item in details) + "."
        explanation = clean_text(quiz.get("explanation"))
        # Do not repeat an evidence sentence already used as the reference answer.
        if explanation and normalize_answer(explanation) != normalize_answer(reference):
            reasoning += " " + explanation
        return {
            "is_correct": correct, "score": 100 if correct else 0,
            "reasoning": reasoning,
            "correct_answer": [options[index] for index in expected] if len(expected) > 1 else options[expected[0]],
            "missing_answers": missing, "extra_answers": extra,
            "citations": [], "insufficient_evidence": False,
            "grading_source": "local_objective",
        }
    if quiz_type == "matching":
        submitted = answer if isinstance(answer, list) else []
        expected = [int(item) for item in quiz["correct_mapping"]]
        parsed: list[int] = []
        for item in submitted:
            try:
                parsed.append(int(item))
            except (TypeError, ValueError):
                parsed.append(-1)
        matched = sum(1 for index, value in enumerate(expected) if index < len(parsed) and parsed[index] == value)
        score = round(100 * matched / len(expected)) if expected else 0
        correct_pairs = [
            f"{term} → {quiz['right'][expected[index]]}"
            for index, term in enumerate(quiz.get("left", []))
            if index < len(expected) and 0 <= expected[index] < len(quiz.get("right", []))
        ]
        pair_feedback = []
        for index, term in enumerate(quiz.get("left", [])):
            target = expected[index]
            selected = parsed[index] if index < len(parsed) else -1
            chosen = quiz["right"][selected] if 0 <= selected < len(quiz["right"]) else "No valid match"
            pair_feedback.append({
                "term": term, "selected": chosen, "correct_answer": quiz["right"][target],
                "is_correct": selected == target,
                "reasoning": (f"{term} → {quiz['right'][target]}" if selected == target else
                              f"For {term}, you chose {chosen.rstrip('. ')}; the supported match is {quiz['right'][target].rstrip('. ')}."),
            })
        return {
            "is_correct": score == 100, "score": score,
            "reasoning": f"{matched} of {len(expected)} pairs are correct. " + " ".join(
                item["reasoning"] for item in pair_feedback if not item["is_correct"]
            ) + (" " + clean_text(quiz.get("explanation")) if quiz.get("explanation") else ""),
            "pair_feedback": pair_feedback,
            "correct_answer": correct_pairs, "citations": [], "insufficient_evidence": False,
            "grading_source": "local_objective",
        }
    if quiz_type == "fill_blank":
        expected = normalize_answer(quiz["answer"])
        actual = normalize_answer(answer)
        correct = bool(actual) and actual == expected
        return {
            "is_correct": correct, "score": 100 if correct else 0,
            "reasoning": "Correct. The term matches the saved course-note evidence." if correct else "Incorrect. The blank does not match the saved course-note term.",
            "correct_answer": quiz["answer"], "citations": [], "insufficient_evidence": False,
            "grading_source": "local_objective",
        }
    raise ValueError("This question type is not locally objective.")


def _references(citations: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[Reference {index}] File={item.get('source_file')} | Section={item.get('section')} | Page={item.get('page')}\n{item.get('excerpt')}"
        for index, item in enumerate(citations, 1)
    )


def _short_answer_points(quiz: dict[str, Any]) -> list[str]:
    """Return only the explicit v8.0.7 Short Answer rubric.

    Legacy Short Answer drafts and the optional Fill Blank semantic path do not
    contain ``key_points`` and therefore continue to use the previous single-score
    grading contract rather than being silently changed into a multi-point task.
    """
    raw = quiz.get("key_points")
    if not isinstance(raw, list):
        return []
    return [clean_text(item) for item in raw if clean_text(item)][:4]


def grading_prompt(
    question: str,
    student_answer: str,
    reference_answer: str,
    key_points: list[str],
    citations: list[dict[str, Any]],
) -> str:
    if not key_points:
        return f"""TASK: Grade one student answer using only REFERENCE ANSWER and ALLOWED REFERENCES.
Judge semantic equivalence rather than exact wording. Return a score from 0 to 100 and 1-2 complete constructive sentences.
Do not use outside knowledge. If evidence is insufficient, set insufficient_evidence=true and use a conservative score.
QUESTION: {question}
STUDENT ANSWER: {student_answer}
REFERENCE ANSWER: {reference_answer}
ALLOWED REFERENCE IDS: {list(range(1, len(citations) + 1))}

ALLOWED REFERENCES:
{_references(citations)}

Return only this JSON schema:
{{"is_correct":false,"score":0,"reasoning":"1-2 complete sentences","correct_answer":"complete reference answer","citations":[1],"insufficient_evidence":false}}
"""
    point_text = "\n".join(f"{index}. {point}" for index, point in enumerate(key_points, 1))
    return f"""TASK: Grade one Short Answer using only REFERENCE ANSWER, REQUIRED KEY POINTS and ALLOWED REFERENCES.
Assess every required point separately. Do not award full marks when one or more required points are missing.
For each key point use only one credit: 100 fully covered, 50 partly covered, or 0 absent/incorrect.
Judge the content required by each key point itself. A point that only lists applications/examples is fully covered when those listed items are correctly stated; do not demand an invented explanation.
The overall score will be recalculated and consistency-checked by the backend.
Write 2-4 complete constructive sentences. Do not end mid-sentence or with a dangling article/conjunction.
Do not use outside knowledge. If evidence is insufficient, set insufficient_evidence=true and score conservatively.
QUESTION: {question}
STUDENT ANSWER: {student_answer}
REFERENCE ANSWER: {reference_answer}
REQUIRED KEY POINTS:
{point_text}
ALLOWED REFERENCE IDS: {list(range(1, len(citations) + 1))}

ALLOWED REFERENCES:
{_references(citations)}

Return only this JSON schema:
{{"is_correct":false,"score":0,"reasoning":"2-4 complete sentences","correct_answer":"complete reference answer","key_point_results":[{{"index":1,"credit":0,"comment":"complete sentence"}}],"citations":[1],"insufficient_evidence":false}}
"""


def _complete_sentence(value: object) -> bool:
    text = clean_text(value)
    if len(text) < 20 or text[-1:] not in {".", "!", "?"}:
        return False
    if re.search(r"\b(?:a|an|the|and|or|but|because|according to)\s*[.!?]?$", text, re.IGNORECASE):
        return False
    last = next((part.strip() for part in reversed(re.split(r"(?<=[.!?])\s+", text)) if part.strip()), "")
    # Reject a grammatically closed but still fragmentary tail such as
    # “According to the course material, an LBS.”
    predicate = re.search(
        r"\b(?:is|are|was|were|be|been|has|have|had|does|do|did|covers?|omits?|"
        r"explains?|states?|shows?|matches?|provides?|uses?|describes?|includes?|"
        r"identifies?|addresses?|supports?|requires?|mentions?|defines?|means?|"
        r"correctly|incorrectly|missing|covered|supported|omitted)\b",
        last,
        re.IGNORECASE,
    )
    return bool(predicate)


def _credit_band(value: object) -> int:
    """Map an Agent/local estimate to the documented 0/50/100 rubric."""
    score = clamp_int(value, 0, 100)
    if score >= 75:
        return 100
    if score >= 25:
        return 50
    return 0


def _point_comment(credit: int) -> str:
    if credit >= 100:
        return "The answer fully covers this required course-note point."
    if credit >= 50:
        return "The answer partly covers this required course-note point but misses some detail."
    return "The answer does not cover this required course-note point sufficiently."


def _normalise_point_results(payload: dict[str, Any], key_points: list[str]) -> list[dict[str, Any]]:
    if not key_points:
        return []
    raw = payload.get("key_point_results")
    if not isinstance(raw, list):
        raise ValueError("Key-point grading results are missing.")
    indexed: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not (1 <= index <= len(key_points)) or index in indexed:
            continue
        comment = clean_text(item.get("comment"))
        if not _complete_sentence(comment):
            raise ValueError("A key-point grading comment is incomplete.")
        indexed[index] = {
            "index": index,
            "point": key_points[index - 1],
            "credit": _credit_band(item.get("credit")),
            "comment": comment,
        }
    if len(indexed) != len(key_points):
        raise ValueError("The grading Agent did not assess every required key point.")
    return [indexed[index] for index in range(1, len(key_points) + 1)]


def normalize_grading(
    raw: object,
    reference_answer: str,
    citation_count: int,
    key_points: list[str] | None = None,
    student_answer: str = "",
) -> dict[str, Any]:
    payload = parse_json_like(raw)
    if not isinstance(payload, dict):
        raise ValueError("Grading output is not an object.")
    points = [clean_text(item) for item in (key_points or []) if clean_text(item)]
    citations: list[int] = []
    for item in payload.get("citations", []) if isinstance(payload.get("citations"), list) else []:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= citation_count and number not in citations:
            citations.append(number)
    insufficient = coerce_bool(payload.get("insufficient_evidence"), False)
    reasoning = clean_text(payload.get("reasoning") or payload.get("feedback"))
    if not _complete_sentence(reasoning):
        raise ValueError("Grading reasoning is missing or incomplete.")
    point_results = _normalise_point_results(payload, points)
    if point_results:
        # The external Agent is useful for paraphrases, but its numerical score is
        # not allowed to under-mark an answer that visibly contains a required
        # course-note point. A deterministic lexical guardrail upgrades only
        # obvious 0/50/100 cases; it never lowers the Agent's semantic credit.
        point_token_sets = [set(tokens(point)) for point in points]
        frequencies = Counter(token for point_tokens in point_token_sets for token in point_tokens)
        for index, item in enumerate(point_results):
            distinctive = {token for token in point_token_sets[index] if frequencies[token] == 1}
            local_credit = _credit_band(_point_credit(student_answer, item["point"], distinctive))
            item["credit"] = max(int(item["credit"]), local_credit)
            item["comment"] = _point_comment(int(item["credit"]))
        score = round(sum(item["credit"] for item in point_results) / len(point_results))
        full = sum(1 for item in point_results if item["credit"] == 100)
        partial = sum(1 for item in point_results if item["credit"] == 50)
        missing = len(point_results) - full - partial
        if full == len(point_results):
            reasoning = f"Your answer fully covers all {len(point_results)} required course-note points."
        else:
            reasoning = (
                f"Your answer fully covers {full} required point(s), partly covers {partial}, "
                f"and misses {missing}."
            )
    else:
        score = clamp_int(payload.get("score"), 0, 100)
    if insufficient:
        score = min(score, 50)
    return {
        "is_correct": score >= 65,
        "score": score,
        "reasoning": reasoning,
        # The authoritative answer is the complete server-side rubric, not a
        # possibly shortened copy returned by the external Agent.
        "correct_answer": clean_text(reference_answer),
        "key_point_results": point_results,
        "citations": citations,
        "insufficient_evidence": insufficient,
        "grading_source": "coze_grading_agent",
    }


def _point_credit(student_answer: str, point: str, distinctive_terms: set[str] | None = None) -> int:
    actual, expected = normalize_answer(student_answer), normalize_answer(point)
    if not actual or not expected:
        return 0
    expected_terms, actual_terms = set(tokens(expected)), set(tokens(actual))
    targets = set(distinctive_terms or expected_terms) or expected_terms
    overlap = targets & actual_terms
    coverage = len(overlap) / max(1, len(targets))
    sequence = SequenceMatcher(None, actual, expected).ratio()
    # Repeated course labels (for example “LBS” or “location”) must not make an
    # omitted bullet look covered.  Without any point-distinctive term, only a
    # small similarity allowance is possible in the local fallback.
    if distinctive_terms and not overlap:
        return min(20, round(20 * sequence))
    return max(0, min(100, round(100 * (0.82 * coverage + 0.18 * sequence))))


def local_semantic_grade(
    student_answer: str,
    reference_answer: str,
    citation_count: int,
    key_points: list[str] | None = None,
) -> dict[str, Any]:
    explicit_points = [clean_text(item) for item in (key_points or []) if clean_text(item)]
    if not explicit_points:
        actual, expected = normalize_answer(student_answer), normalize_answer(reference_answer)
        if not actual:
            score = 0
        else:
            expected_terms, actual_terms = set(tokens(expected)), set(tokens(actual))
            coverage = len(expected_terms & actual_terms) / max(1, len(expected_terms))
            precision = len(expected_terms & actual_terms) / max(1, len(actual_terms))
            sequence = SequenceMatcher(None, actual, expected).ratio()
            score = round(100 * (0.55 * coverage + 0.20 * precision + 0.25 * sequence))
        correct = score >= 65
        return {
            "is_correct": correct,
            "score": score,
            "reasoning": (
                "Your answer covers the main course-note points."
                if correct else
                "Your answer omits or differs from important course-note points."
            ),
            "correct_answer": reference_answer,
            "key_point_results": [],
            "citations": [1] if citation_count else [],
            "insufficient_evidence": citation_count == 0,
            "grading_source": "local_fallback",
        }

    points = explicit_points
    point_token_sets = [set(tokens(point)) for point in points]
    frequencies = Counter(token for point_tokens in point_token_sets for token in point_tokens)
    point_results: list[dict[str, Any]] = []
    for index, point in enumerate(points, 1):
        distinctive = {token for token in point_token_sets[index - 1] if frequencies[token] == 1}
        credit = _credit_band(_point_credit(student_answer, point, distinctive))
        point_results.append({
            "index": index, "point": point, "credit": credit,
            "comment": _point_comment(credit),
        })
    score = round(sum(item["credit"] for item in point_results) / max(1, len(point_results)))
    correct = score >= 65
    covered = sum(1 for item in point_results if item["credit"] == 100)
    partial = sum(1 for item in point_results if item["credit"] == 50)
    missing = len(point_results) - covered - partial
    reasoning = (
        f"The local fallback found {covered} fully covered point(s), "
        f"{partial} partly covered point(s), and {missing} missing point(s)."
    )
    return {
        "is_correct": correct,
        "score": score,
        "reasoning": reasoning,
        "correct_answer": reference_answer,
        "key_point_results": point_results,
        "citations": list(range(1, min(citation_count, len(points)) + 1)),
        "insufficient_evidence": citation_count == 0,
        "grading_source": "local_fallback",
    }


def grade_short_answer(quiz: dict[str, Any], student_answer: str, citations: list[dict[str, Any]], router: CozeAgentRouter, user_id: str) -> dict[str, Any]:
    reference = clean_text(quiz.get("reference"))
    key_points = _short_answer_points(quiz)
    if router.config(AgentKind.GRADING).configured:
        try:
            response = router.request(
                AgentKind.GRADING,
                grading_prompt(quiz.get("question", ""), student_answer, reference, key_points, citations),
                user_id,
            )
            return normalize_grading(
                response.content, reference, len(citations), key_points, student_answer
            )
        except (CozeTransportError, ValueError):
            pass
    return local_semantic_grade(student_answer, reference, len(citations), key_points)

def grade_fill_with_optional_agent(quiz: dict[str, Any], student_answer: str, citations: list[dict[str, Any]], router: CozeAgentRouter, user_id: str) -> dict[str, Any]:
    local = grade_objective(quiz, student_answer)
    if local["is_correct"] or not router.config(AgentKind.GRADING).configured:
        return local
    # Only ambiguous non-empty variants are sent; blank/wildly different answers stay local.
    if not normalize_answer(student_answer) or jaccard(student_answer, quiz.get("answer")) < 0.35:
        return local
    synthetic = {"question": quiz.get("question", ""), "reference": quiz.get("answer", "")}
    return grade_short_answer(synthetic, student_answer, citations, router, user_id)

