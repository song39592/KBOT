from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from typing import Any

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from common.auth import student_login_required
from common.feedback_builder import build_detailed_feedback, build_review_payload
from common.grading_engine import grade_fill_with_optional_agent, grade_objective, grade_short_answer, local_semantic_grade
from common.knowledge_client import KnowledgeServiceError
from common.models import QuizSession, StudentQARecord, db
from common.question_bank import pick_question
from common.quiz_engine import QUIZ_TYPES, QuizUnavailableError, generate_quiz
from common.quiz_parser import QuizValidationError
from common.quiz_flow import current_type as normalize_flow_type, request_matches_current, skip_transition, transition
from student_app.services import agent_router, knowledge_client

bp = Blueprint("exercise", __name__)
LABELS = {
    "true_false": "True/False",
    "mcq": "MCQ",
    "matching": "Matching",
    "fill_blank": "Fill Blank",
    "short_answer": "Short Answer",
}




def _public_quiz(quiz: dict[str, Any]) -> dict[str, Any]:
    """Return only fields needed to render a question before submission.

    Answer keys, reference answers, explanations, mappings and citations remain
    server-side until the attempt has been graded.
    """
    quiz_type = str(quiz.get("type") or "")
    public = {"type": quiz_type, "question": str(quiz.get("question") or "")}
    if quiz_type in {"true_false", "mcq"}:
        public["options"] = list(quiz.get("options") or [])
    elif quiz_type == "matching":
        public["left"] = list(quiz.get("left") or [])
        public["right"] = list(quiz.get("right") or [])
    return public


def _loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _record() -> QuizSession:
    record_id = str(session.get("quiz_session_id") or uuid.uuid4().hex)
    session["quiz_session_id"] = record_id
    record = db.session.get(QuizSession, record_id)
    if record is None:
        record = QuizSession(id=record_id, student_name=session["student_username"])
        db.session.add(record)
        db.session.commit()
    return record


def _history(record: QuizSession) -> dict[str, list]:
    return {
        "knowledge": _loads(record.used_knowledge_json, []),
        "chunks": _loads(record.used_chunks_json, []),
        "pages": _loads(record.used_pages_json, []),
        "facts": _loads(record.used_facts_json, []),
        "fingerprints": _loads(record.used_fingerprints_json, []),
    }


def _has_saved_quiz(record: QuizSession, quiz_type: str | None = None) -> bool:
    draft = _loads(record.current_quiz_json, {})
    if not isinstance(draft, dict) or not draft.get("quiz_id") or not isinstance(draft.get("quiz"), dict):
        return False
    return not quiz_type or str(draft.get("quiz_type") or "") == quiz_type


def _has_validated_topic(record: QuizSession) -> bool:
    # History is appended only after a grounded question has been generated.
    # A skip is permitted only after topic-scoped retrieval succeeded and both
    # primary and local recovery generation failed, so an audited skip also
    # validates the locked topic.
    return (
        _has_saved_quiz(record)
        or bool(_history(record).get("facts"))
        or bool(_loads(getattr(record, "skipped_types_json", "[]"), []))
    )


def _append_history(record: QuizSession, generated) -> None:
    history = _history(record)
    history["knowledge"].append(generated.focus.chunk.get("knowledge_key", ""))
    history["chunks"].append(generated.focus.chunk.get("chunk_id", ""))
    history["pages"].append(generated.focus.chunk.get("page"))
    history["facts"].append(generated.focus.fact_key)
    history["fingerprints"].append(generated.question_fingerprint)
    record.used_knowledge_json = json.dumps([item for item in history["knowledge"] if item][-100:])
    record.used_chunks_json = json.dumps([item for item in history["chunks"] if item][-100:])
    record.used_pages_json = json.dumps([item for item in history["pages"] if item is not None][-100:])
    record.used_facts_json = json.dumps([item for item in history["facts"] if item][-100:])
    record.used_fingerprints_json = json.dumps([item for item in history["fingerprints"] if item][-100:])
    if generated.coverage_exhausted:
        record.exhausted_count += 1



def _generation_failures(record: QuizSession) -> dict[str, dict[str, Any]]:
    value = _loads(getattr(record, "generation_failures_json", "{}"), {})
    return value if isinstance(value, dict) else {}


def _generation_marker(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == "generating" and bool(value.get("request_id"))


def _claim_generation(record: QuizSession, quiz_type: str, topic: str, request_id: str) -> tuple[str, dict[str, Any], str]:
    """Atomically claim one generation job across tabs/process workers.

    ``current_quiz_json`` doubles as a short-lived generation marker.  This
    avoids two simultaneous requests spending money on Coze and prevents the
    later request from overwriting the first question.  A stale marker is
    reclaimable after the configured TTL.
    """
    existing_raw = str(record.current_quiz_json or "{}")
    existing = _loads(existing_raw, {})
    if _has_saved_quiz(record, quiz_type):
        return "saved", existing, ""
    if _generation_marker(existing):
        started = float(existing.get("started_at") or 0.0)
        ttl = float(current_app.config.get("GENERATION_LOCK_TTL", 90.0))
        if started and time.time() - started < ttl:
            return "pending", existing, ""

    marker = {
        "status": "generating",
        "request_id": request_id,
        "quiz_type": quiz_type,
        "topic": topic,
        "started_at": time.time(),
    }
    marker_raw = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    updated = (
        db.session.query(QuizSession)
        .filter(QuizSession.id == record.id, QuizSession.current_quiz_json == existing_raw)
        .update({QuizSession.current_quiz_json: marker_raw}, synchronize_session=False)
    )
    db.session.commit()
    if updated == 1:
        record.current_quiz_json = marker_raw
        return "claimed", marker, marker_raw

    db.session.expire_all()
    latest = db.session.get(QuizSession, record.id)
    latest_value = _loads(latest.current_quiz_json if latest else "{}", {})
    if latest and _has_saved_quiz(latest, quiz_type):
        return "saved", latest_value, ""
    return "pending", latest_value, ""


def _release_generation_claim(record_id: str, marker_raw: str) -> QuizSession:
    if marker_raw:
        (
            db.session.query(QuizSession)
            .filter(QuizSession.id == record_id, QuizSession.current_quiz_json == marker_raw)
            .update({QuizSession.current_quiz_json: "{}"}, synchronize_session=False)
        )
        db.session.commit()
    db.session.expire_all()
    return db.session.get(QuizSession, record_id)


def _record_generation_failure(record: QuizSession, quiz_type: str, topic: str, reason: str, *, skippable: bool) -> int:
    failures = _generation_failures(record)
    previous = failures.get(quiz_type) if isinstance(failures.get(quiz_type), dict) else {}
    count = int(previous.get("count") or 0) + 1
    failures[quiz_type] = {
        "count": count,
        "topic": topic,
        "reason": str(reason or "Question generation was unavailable.")[:500],
        "skippable": bool(skippable),
        "updated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
    }
    record.generation_failures_json = json.dumps(failures, ensure_ascii=False)
    db.session.commit()
    return count


def _clear_generation_failure(record: QuizSession, quiz_type: str) -> None:
    failures = _generation_failures(record)
    if quiz_type in failures:
        failures.pop(quiz_type, None)
        record.generation_failures_json = json.dumps(failures, ensure_ascii=False)


def _generation_response(existing: dict[str, Any], current: str, topic: str) -> dict[str, Any]:
    from_bank = bool(existing.get("from_question_bank"))
    # Bank-first path: bank hits keep the bank badge even if the stored row was
    # originally produced by an agent. Any non-bank serve (Coze or local
    # recovery) is labeled as AI-generated for the student UI.
    generated_by_ai = not from_bank
    if from_bank:
        source_label = "From question bank"
    else:
        source_label = "Generated by AI"
    return {
        "ok": True,
        "quiz_id": existing["quiz_id"],
        "quiz": _public_quiz(existing["quiz"]),
        "quiz_type": current,
        "topic": existing.get("topic") if existing.get("topic") is not None else topic,
        "generated_by_ai": generated_by_ai,
        "from_question_bank": from_bank,
        "source_label": source_label,
        "diversity_notice": (
            "The relevant unused knowledge pool is exhausted, so a related fact may repeat."
            if existing.get("coverage_exhausted") else ""
        ),
    }


def _draft_from_bank_hit(hit: dict[str, Any], quiz_type: str, topic: str) -> dict[str, Any]:
    quiz = hit.get("quiz") if isinstance(hit.get("quiz"), dict) else None
    assert quiz is not None
    citations = hit.get("citations") if isinstance(hit.get("citations"), list) else []
    chunk = hit.get("chunk") if isinstance(hit.get("chunk"), dict) else {}
    bank_topic = topic if topic else str(hit.get("topic") or "").strip()
    return {
        "quiz_id": uuid.uuid4().hex,
        "quiz": quiz,
        "generation_source": str(hit.get("generation_source") or "question_bank"),
        "from_question_bank": True,
        "focus_text": str(hit.get("focus_text") or hit.get("source_excerpt") or ""),
        "fact_key": str(hit.get("fact_key") or ""),
        "chunk": chunk,
        "citations": citations,
        "topic": bank_topic,
        "quiz_type": quiz_type,
        "fingerprint": str(hit.get("fingerprint") or f"bank-{uuid.uuid4().hex[:12]}"),
        "coverage_exhausted": False,
    }


def _append_bank_history(record: QuizSession, draft: dict[str, Any]) -> None:
    history = _history(record)
    chunk = draft.get("chunk") if isinstance(draft.get("chunk"), dict) else {}
    history["knowledge"].append(str(chunk.get("knowledge_key") or ""))
    history["chunks"].append(str(chunk.get("chunk_id") or ""))
    history["pages"].append(chunk.get("page"))
    history["facts"].append(str(draft.get("fact_key") or ""))
    history["fingerprints"].append(str(draft.get("fingerprint") or ""))
    record.used_knowledge_json = json.dumps([item for item in history["knowledge"] if item][-100:])
    record.used_chunks_json = json.dumps([item for item in history["chunks"] if item][-100:])
    record.used_pages_json = json.dumps([item for item in history["pages"] if item is not None][-100:])
    record.used_facts_json = json.dumps([item for item in history["facts"] if item][-100:])
    record.used_fingerprints_json = json.dumps([item for item in history["fingerprints"] if item][-100:])


def _search_limits(quiz_type: str, recovery: bool = False) -> tuple[int, int]:
    normal = {
        "true_false": (8, 0),
        "fill_blank": (10, 0),
        "mcq": (12, 20),
        "matching": (14, 24),
        "short_answer": (16, 0),
    }.get(quiz_type, (12, 0))
    if not recovery:
        return normal
    return min(20, max(normal[0] + 6, 16)), min(30, max(normal[1] + 6, 24)) if normal[1] else 0


def _is_open_ended_topic(topic: str, chapter_label: str = "") -> bool:
    """True when the learner asked for chapter-wide random generation.

    Treat blank input and accidental chapter-title submissions the same: using
    the chapter label as a lexical topic makes True/False matching fail because
    notes rarely contain the phrase \"Chapter 3: Enabling Technologies\".
    """
    value = str(topic or "").strip()
    if not value:
        return True
    chapter = str(chapter_label or "").strip()
    if chapter and value.casefold() == chapter.casefold():
        return True
    return value.casefold() in {"course material", "course materials", "this chapter", "chapter"}


def _derived_topic_from_generated(generated: Any, chapter_label: str = "") -> str:
    """Pick a short lockable topic after chapter-wide (empty-input) generation."""
    chunk = generated.focus.chunk if getattr(generated, "focus", None) else {}
    if not isinstance(chunk, dict):
        chunk = {}
    section = str(chunk.get("section") or "").strip()
    if section and len(section) <= 120 and not _is_open_ended_topic(section, chapter_label):
        return section[:240]
    text = str(getattr(getattr(generated, "focus", None), "text", "") or "").strip()
    words = [part for part in re.findall(r"[A-Za-z][A-Za-z0-9'/-]*", text) if len(part) > 2][:4]
    if words:
        return " ".join(words)[:240]
    # Prefer a concrete course keyword over re-locking the chapter title.
    return "course content"


def _generate_with_recovery(
    quiz_type: str,
    topic: str,
    chapter_id: str,
    history: dict[str, list],
    record_id: str,
):
    """Generate once with configured Agent routing, then deterministic reuse.

    The recovery pass removes diversity exclusions, expands the retrieval pool
    and disables another external Agent call.  Therefore a learner is not asked
    to keep clicking Generate after the evidence pool is exhausted or an Agent
    response is unusable.

    An empty ``topic`` means chapter-wide random generation: search the chapter
    without topic_mode filtering so True/False can use any usable statement.
    """
    timings: dict[str, float] = {}
    last_error: Exception | None = None
    found_topic_evidence = False
    chapter_wide = not bool(str(topic or "").strip())
    for recovery in (False, True):
        pass_started = time.perf_counter()
        search_history = history
        if recovery:
            search_history = {"knowledge": [], "chunks": [], "pages": [], "facts": [], "fingerprints": history.get("fingerprints", [])}
        elif quiz_type == "short_answer":
            search_history = {**history, "knowledge": [], "chunks": [], "pages": []}

        search_limit, term_limit = _search_limits(quiz_type, recovery)
        if chapter_wide:
            # Empty-topic True/False needs a wider unused-statement pool; the
            # highest-ranked empty-query chunks are often titles/discussion slides.
            search_limit = max(search_limit, 20 if recovery else 16)
        search = knowledge_client().search(
            topic if not chapter_wide else "",
            chapter_id,
            search_limit,
            search_history,
            topic_mode=not chapter_wide,
            neighbor_pages=5 if quiz_type == "matching" else 0,
        )
        timings[f"search_{'recovery' if recovery else 'primary'}"] = time.perf_counter() - pass_started
        if not search.get("results"):
            if recovery:
                break
            continue
        found_topic_evidence = True
        terms_started = time.perf_counter()
        terms = (
            knowledge_client().matching_candidates(topic, chapter_id, term_limit)
            if term_limit and quiz_type in {"mcq", "matching"}
            else []
        )
        timings[f"terms_{'recovery' if recovery else 'primary'}"] = time.perf_counter() - terms_started
        generation_started = time.perf_counter()
        try:
            generated = generate_quiz(
                quiz_type,
                topic,
                search,
                terms,
                search_history,
                agent_router(),
                session["student_username"],
                generation_seed=record_id,
                allow_agent=not recovery,
                local_source="local_recovery" if recovery else "local_fallback",
            )
            timings[f"generation_{'recovery' if recovery else 'primary'}"] = time.perf_counter() - generation_started
            return generated, timings, found_topic_evidence
        except (QuizUnavailableError, QuizValidationError) as exc:
            timings[f"generation_{'recovery' if recovery else 'primary'}"] = time.perf_counter() - generation_started
            last_error = exc
            continue

    if not found_topic_evidence:
        raise QuizUnavailableError("No relevant course-note content was found for this topic.")
    if last_error is not None:
        raise last_error
    raise QuizUnavailableError("The relevant course notes cannot support this question type safely.")

def _current_type() -> str:
    return normalize_flow_type(session.get("quiz_current_type"))


def _reset_flow(chapter_id: str, chapter_label: str) -> QuizSession:
    session["chapter_id"] = chapter_id
    session["chapter_label"] = chapter_label
    session["quiz_current_type"] = "true_false"
    session["quiz_topic"] = ""
    session["quiz_complete"] = False
    session["quiz_session_id"] = uuid.uuid4().hex
    record = _record()
    record.chapter_id = chapter_id
    record.used_knowledge_json = "[]"
    record.used_chunks_json = "[]"
    record.used_pages_json = "[]"
    record.used_facts_json = "[]"
    record.used_fingerprints_json = "[]"
    record.current_quiz_json = "{}"
    record.generation_failures_json = "{}"
    record.skipped_types_json = "[]"
    record.exhausted_count = 0
    db.session.commit()
    return record


@bp.get("/setup")
@student_login_required
def legacy_setup():
    return redirect(url_for("exercise.setup_page"))


@bp.get("/exercise/setup")
@student_login_required
def setup_page():
    chapters, error = [], ""
    try:
        chapters = knowledge_client().chapters()
    except KnowledgeServiceError:
        error = "Course materials are temporarily unavailable."
    return render_template("exercise_setup.html", chapters=chapters, knowledge_error=error)


@bp.post("/api/exercise/setup")
@student_login_required
def setup_action():
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    chapter_id = str(data.get("chapter_id") or data.get("chapter") or "").strip()
    try:
        chapters = {str(item.get("id")): item for item in knowledge_client().chapters()}
    except KnowledgeServiceError:
        return jsonify({"ok": False, "error": "Course materials are temporarily unavailable."}), 503
    if chapter_id not in chapters:
        return jsonify({"ok": False, "error": "Select an available course chapter."}), 400
    chapter = chapters[chapter_id]
    _reset_flow(chapter_id, str(chapter.get("label") or ""))
    return jsonify({"ok": True, "redirect": url_for("exercise.quiz_page", quiz_type="true_false")})


@bp.get("/exercise")
@student_login_required
def exercise_root():
    if not session.get("chapter_id"):
        return redirect(url_for("exercise.setup_page"))
    return redirect(url_for("exercise.quiz_page", quiz_type=_current_type()))


@bp.get("/quiz/<quiz_type>")
@bp.get("/exercise/<quiz_type>")
@student_login_required
def quiz_page(quiz_type: str):
    if not session.get("chapter_id"):
        return redirect(url_for("exercise.setup_page"))
    if session.get("quiz_complete"):
        return redirect(url_for("exercise.setup_page"))

    requested = normalize_flow_type(quiz_type)
    current = _current_type()
    # The URL is not a type selector. A student cannot skip or revisit steps by
    # editing the address or clicking a progress item.
    if not request_matches_current(requested, current):
        return redirect(url_for("exercise.quiz_page", quiz_type=current))

    topic = str(session.get("quiz_topic") or "")
    # v8.0.7 stored the first submitted topic before retrieval succeeded.  A
    # failed out-of-scope request could therefore leave a stale locked topic in
    # the browser session.  Clear that legacy state when no question draft was
    # ever saved, so the input becomes editable again after an upgrade/reload.
    # Also clear a locked chapter title — it is not a usable True/False topic.
    if topic and _is_open_ended_topic(topic, str(session.get("chapter_label") or "")):
        session.pop("quiz_topic", None)
        topic = ""
    if current == "true_false" and topic:
        record = _record()
        if not _has_validated_topic(record):
            session.pop("quiz_topic", None)
            topic = ""
    return render_template(
        "exercise_quiz.html",
        quiz_type=current,
        quiz_type_label=LABELS[current],
        label=LABELS[current],
        labels=LABELS,
        types=QUIZ_TYPES,
        quiz_flow=[(item, LABELS[item]) for item in QUIZ_TYPES],
        current_flow_index=QUIZ_TYPES.index(current),
        chapter=session.get("chapter_label", ""),
        topic=topic,
        topic_locked=bool(topic),
        auto_generate=bool(topic and request.args.get("autogenerate") == "1"),
    )


@bp.post("/api/exercise/generate")
@student_login_required
def generate_action():
    data = request.get_json(silent=True) or {}
    chapter_id = str(session.get("chapter_id") or "")
    current = _current_type()
    requested = normalize_flow_type(data.get("quiz_type") or current)
    request_id = uuid.uuid4().hex[:8]
    request_started_at = time.perf_counter()
    current_app.logger.info(
        "Exercise generate start request=%s current=%s requested=%s",
        request_id, current, requested,
    )
    if not chapter_id:
        return jsonify({"ok": False, "error": "Select a course chapter before starting."}), 400
    if session.get("quiz_complete"):
        return jsonify({"ok": False, "error": "This quiz flow is complete. Start a new session."}), 409
    if not request_matches_current(requested, current):
        return jsonify({"ok": False, "error": "Complete the current question type before moving on.", "current_type": current}), 409

    locked_topic = str(session.get("quiz_topic") or "").strip()
    # Empty topic is allowed: prefer the question bank, then fall back to AI.
    submitted_topic = str(data.get("topic") or "").strip()[:240]
    record = _record()
    topic_is_locked = bool(locked_topic and _has_validated_topic(record))
    topic = locked_topic if topic_is_locked else submitted_topic
    chapter_label = str(session.get("chapter_label") or "")
    # Chapter title must not act as a lexical topic filter for True/False.
    open_ended = _is_open_ended_topic(topic, chapter_label)
    if open_ended and not topic_is_locked:
        topic = ""
        session.pop("quiz_topic", None)

    existing = _loads(record.current_quiz_json, {})
    if _has_saved_quiz(record, current):
        current_app.logger.info(
            "Exercise generate reused request=%s type=%s total=%.3fs",
            request_id, current, time.perf_counter() - request_started_at,
        )
        return jsonify(_generation_response(existing, current, topic))

    claim_status, claim_value, marker_raw = _claim_generation(record, current, topic, request_id)
    if claim_status == "saved":
        return jsonify(_generation_response(claim_value, current, topic))
    if claim_status == "pending":
        return jsonify({
            "ok": True,
            "pending": True,
            "quiz_type": current,
            "topic": topic,
            "retry_after_ms": 700,
            "message": "This question is already being generated. Waiting for the same result…",
        }), 202

    record_id = record.id
    history = _history(record)

    # Prefer teacher question bank (Excel BankQuestion, then reserved JSON bank).
    bank_hit = pick_question(
        chapter_id,
        current,
        topic,
        history.get("fingerprints", []),
        chapter_label=chapter_label,
    )
    if bank_hit is not None:
        bank_topic = topic if topic else str(bank_hit.get("topic") or "").strip()
        draft = _draft_from_bank_hit(bank_hit, current, bank_topic)
        draft_raw = json.dumps(draft, ensure_ascii=False)
        updated = (
            db.session.query(QuizSession)
            .filter(QuizSession.id == record_id, QuizSession.current_quiz_json == marker_raw)
            .update({QuizSession.current_quiz_json: draft_raw}, synchronize_session=False)
        )
        if updated != 1:
            db.session.rollback()
            current_app.logger.error(
                "Bank ownership was lost request=%s session=%s", request_id, record_id,
            )
            return jsonify({"ok": False, "error": "The question request was superseded. Please retry."}), 409
        db.session.commit()
        db.session.expire_all()
        record = db.session.get(QuizSession, record_id)
        if bank_topic:
            session["quiz_topic"] = bank_topic
        _clear_generation_failure(record, current)
        _append_bank_history(record, draft)
        db.session.commit()
        current_app.logger.info(
            "Exercise served from question bank request=%s type=%s topic=%r total=%.3fs",
            request_id, current, bank_topic, time.perf_counter() - request_started_at,
        )
        return jsonify(_generation_response(draft, current, bank_topic))

    # No bank hit: generate (AI / local). Keep generation_topic empty for
    # chapter-wide random retrieval + True/False statement selection.
    generation_topic = "" if open_ended else topic
    try:
        generated, timings, topic_evidence = _generate_with_recovery(
            current, generation_topic, chapter_id, history, record_id,
        )
    except KnowledgeServiceError:
        record = _release_generation_claim(record_id, marker_raw)
        current_app.logger.warning(
            "Exercise generation dependency unavailable request=%s type=%s topic=%r total=%.3fs",
            request_id, current, generation_topic, time.perf_counter() - request_started_at,
        )
        return jsonify({"ok": False, "error": "Course materials are temporarily unavailable."}), 503
    except (QuizUnavailableError, QuizValidationError) as exc:
        record = _release_generation_claim(record_id, marker_raw)
        message = str(exc)
        topic_evidence = "No relevant course-note content" not in message
        # Do not persist the chapter title as the failed topic — that locks the
        # rest of the flow onto an unmatchable lexical filter after Skip.
        failure_topic = generation_topic if generation_topic else ""
        failure_count = _record_generation_failure(
            record, current, failure_topic, message, skippable=topic_evidence,
        )
        current_app.logger.warning(
            "Exercise generation exhausted recovery request=%s type=%s topic=%r open_ended=%s total=%.3fs skippable=%s reason=%s",
            request_id, current, generation_topic, open_ended,
            time.perf_counter() - request_started_at, topic_evidence, message,
        )
        payload = {
            "ok": False,
            "error": message,
            "current_type": current,
            "failure_count": failure_count,
            "can_skip": bool(topic_evidence),
        }
        if topic_evidence:
            payload["recovery_hint"] = "The server already retried with reusable course evidence. You can skip this unavailable type and continue."
        return jsonify(payload), 409
    except Exception:
        record = _release_generation_claim(record_id, marker_raw)
        failure_count = _record_generation_failure(
            record, current, generation_topic,
            "A course-grounded question could not be generated after recovery.",
            skippable=True,
        )
        current_app.logger.exception(
            "Exercise generation failed request=%s type=%s topic=%r elapsed=%.3fs",
            request_id, current, generation_topic, time.perf_counter() - request_started_at,
        )
        return jsonify({
            "ok": False,
            "error": "A course-grounded question could not be generated after the recovery pass.",
            "current_type": current,
            "failure_count": failure_count,
            "can_skip": True,
            "recovery_hint": "You can skip this unavailable type and continue the server-controlled flow.",
        }), 422

    generation_finished_at = time.perf_counter()
    timing_text = " ".join(f"{name}={value:.3f}s" for name, value in sorted(timings.items()))
    current_app.logger.info(
        "Exercise generated request=%s type=%s topic=%r source=%s %s total=%.3fs",
        request_id, current, generation_topic, generated.generation_source, timing_text,
        generation_finished_at - request_started_at,
    )

    # Empty input: lock onto a concrete topic derived from the chosen evidence
    # so later stages in the five-type flow stay coherent.
    if open_ended:
        locked_topic_value = _derived_topic_from_generated(
            generated, str(session.get("chapter_label") or ""),
        )
    else:
        locked_topic_value = topic
    quiz_id = uuid.uuid4().hex
    draft = {
        "quiz_id": quiz_id,
        "quiz": generated.quiz,
        "generation_source": generated.generation_source,
        "from_question_bank": False,
        "focus_text": generated.focus.text,
        "fact_key": generated.focus.fact_key,
        "chunk": generated.focus.chunk,
        "citations": generated.citations,
        "topic": locked_topic_value,
        "quiz_type": current,
        "fingerprint": generated.question_fingerprint,
        "coverage_exhausted": generated.coverage_exhausted,
    }
    # The generation marker is the cross-process ownership token.  Only this
    # request can replace it with the final draft.
    draft_raw = json.dumps(draft, ensure_ascii=False)
    updated = (
        db.session.query(QuizSession)
        .filter(QuizSession.id == record_id, QuizSession.current_quiz_json == marker_raw)
        .update({QuizSession.current_quiz_json: draft_raw}, synchronize_session=False)
    )
    if updated != 1:
        db.session.rollback()
        current_app.logger.error("Generation ownership was lost request=%s session=%s", request_id, record_id)
        return jsonify({"ok": False, "error": "The question generation request was superseded. Please retry."}), 409
    db.session.commit()
    db.session.expire_all()
    record = db.session.get(QuizSession, record_id)
    session["quiz_topic"] = locked_topic_value
    _clear_generation_failure(record, current)
    _append_history(record, generated)
    db.session.commit()
    return jsonify(_generation_response(draft, current, locked_topic_value))


@bp.post("/api/exercise/skip")
@student_login_required
def skip_unavailable_stage():
    """Advance only after exhaustive generation recovery has failed."""
    data = request.get_json(silent=True) or {}
    current = _current_type()
    requested = normalize_flow_type(data.get("quiz_type") or current)
    if not request_matches_current(requested, current):
        return jsonify({"ok": False, "error": "Only the current question type can be skipped."}), 409
    if session.get("quiz_complete"):
        return jsonify({"ok": False, "error": "This quiz flow is already complete."}), 409

    record = _record()
    active = _loads(record.current_quiz_json, {})
    if _has_saved_quiz(record, current):
        return jsonify({"ok": False, "error": "An active question is available and must be answered."}), 409
    if _generation_marker(active):
        return jsonify({"ok": False, "error": "Question generation is still in progress. Please wait."}), 409

    failures = _generation_failures(record)
    failure = failures.get(current) if isinstance(failures.get(current), dict) else {}
    if not failure or not failure.get("skippable"):
        return jsonify({"ok": False, "error": "This stage has not exhausted its safe generation recovery."}), 409

    topic = str(session.get("quiz_topic") or failure.get("topic") or "").strip()
    if _is_open_ended_topic(topic, str(session.get("chapter_label") or "")):
        topic = ""
    # Empty topic is allowed when the learner started without a topic filter.
    skipped = _loads(getattr(record, "skipped_types_json", "[]"), [])
    if not isinstance(skipped, list):
        skipped = []
    skipped.append({
        "quiz_type": current,
        "topic": topic,
        "reason": str(failure.get("reason") or "Generation unavailable")[:500],
        "time": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
    })
    record.skipped_types_json = json.dumps(skipped[-20:], ensure_ascii=False)
    record.current_quiz_json = "{}"
    _clear_generation_failure(record, current)
    if topic:
        session["quiz_topic"] = topic
    else:
        session.pop("quiz_topic", None)

    flow = skip_transition(current)
    if flow.completed:
        session["quiz_complete"] = True
    else:
        session["quiz_current_type"] = flow.next_type
    db.session.commit()
    redirect_url = (
        url_for("history.history_page")
        if flow.completed
        else url_for("exercise.quiz_page", quiz_type=flow.next_type, autogenerate=1)
    )
    current_app.logger.warning(
        "Exercise stage skipped after exhausted recovery session=%s type=%s topic=%r next=%s",
        record.id, current, topic, flow.next_type,
    )
    return jsonify({
        "ok": True,
        "skipped": True,
        "skipped_type": current,
        "next_type": flow.next_type,
        "completed": flow.completed,
        "redirect": redirect_url,
        "message": "This unavailable question type was skipped after server-side recovery failed.",
    })


@bp.post("/api/exercise/submit")
@student_login_required
def submit_action():
    submit_started_at = time.perf_counter()
    request_id = uuid.uuid4().hex[:8]
    data = request.get_json(silent=True) or {}
    record = _record()
    draft = _loads(record.current_quiz_json, {})
    current = _current_type()
    if not draft or str(data.get("quiz_id") or "") != draft.get("quiz_id"):
        return jsonify({"ok": False, "error": "This question has expired. Generate a new question."}), 409
    if draft.get("quiz_type") != current:
        return jsonify({"ok": False, "error": "This question does not belong to the current quiz step."}), 409

    quiz, student_answer = draft["quiz"], data.get("answer")
    current_app.logger.info(
        "Exercise submit start request=%s type=%s", request_id, quiz.get("type"),
    )
    try:
        if quiz["type"] == "short_answer":
            result = grade_short_answer(quiz, str(student_answer or ""), draft["citations"], agent_router(), session["student_username"])
        elif quiz["type"] == "fill_blank":
            result = grade_fill_with_optional_agent(quiz, str(student_answer or ""), draft["citations"], agent_router(), session["student_username"])
        else:
            result = grade_objective(quiz, student_answer)
    except Exception:
        # Every saved draft has already passed schema and grounding validation,
        # so a transport/parser failure during grading should still finish via
        # a deterministic server-side grader.  This is deliberately broader
        # than the normal grading-engine fallback: it is the final anti-stall
        # boundary for unexpected runtime failures.
        current_app.logger.exception(
            "Primary exercise grading failed; applying defensive local fallback request=%s type=%s elapsed=%.3fs",
            request_id, quiz.get("type"), time.perf_counter() - submit_started_at,
        )
        try:
            if quiz.get("type") == "short_answer":
                result = local_semantic_grade(
                    str(student_answer or ""),
                    str(quiz.get("reference") or ""),
                    len(draft.get("citations") or []),
                    quiz.get("key_points") if isinstance(quiz.get("key_points"), list) else None,
                )
                result["grading_source"] = "local_emergency_fallback"
            else:
                result = grade_objective(quiz, student_answer)
                result["grading_source"] = "local_emergency_fallback"
        except Exception:
            # A corrupted/legacy draft is not left active forever.  Clear it,
            # record an auditable skippable failure, and let the learner retry
            # generation or continue to the next stage.
            current_app.logger.exception(
                "Defensive local grading also failed request=%s type=%s",
                request_id, quiz.get("type"),
            )
            record.current_quiz_json = "{}"
            db.session.commit()
            _record_generation_failure(
                record, current, str(draft.get("topic") or session.get("quiz_topic") or ""),
                "The saved question could not be graded safely after local recovery.",
                skippable=True,
            )
            return jsonify({
                "ok": False,
                "error": "This saved question could not be graded safely after local recovery.",
                "current_type": current,
                "can_skip": True,
                "recovery_hint": "You can retry question generation or skip this unavailable type and continue.",
            }), 422

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
    focus_chunk = draft.get("chunk") or {}
    detailed_feedback = build_detailed_feedback(
        quiz,
        student_answer,
        {**result, "generation_source": draft["generation_source"]},
        draft["citations"],
    )
    db.session.add(StudentQARecord(
        id=draft["quiz_id"],
        student_name=session["student_username"],
        chapter=session.get("chapter_label", ""),
        chapter_id=session.get("chapter_id", ""),
        question_type=draft["quiz_type"],
        topic=draft["topic"],
        question=quiz.get("question", ""),
        answer=json.dumps(student_answer, ensure_ascii=False) if isinstance(student_answer, (list, dict)) else str(student_answer or ""),
        options_or_pairs=json.dumps({key: quiz.get(key) for key in ("options", "correct_index", "left", "right", "correct_mapping", "answer", "reference", "key_points") if key in quiz}, ensure_ascii=False),
        correct_answer=json.dumps(result["correct_answer"], ensure_ascii=False) if isinstance(result["correct_answer"], (list, dict)) else str(result["correct_answer"]),
        is_correct=result["is_correct"],
        score=result["score"],
        ai_feedback=detailed_feedback,
        explanation=quiz.get("explanation", ""),
        generation_source=draft["generation_source"],
        grading_source=result["grading_source"],
        source=draft["generation_source"],
        citations_json=json.dumps(draft["citations"], ensure_ascii=False),
        source_document=focus_chunk.get("source_file", ""),
        source_page=focus_chunk.get("page"),
        source_pages_json=json.dumps(sorted({item.get("page") for item in draft["citations"] if item.get("page") is not None})),
        source_excerpt=draft["focus_text"],
        source_chunk_id=focus_chunk.get("chunk_id", ""),
        knowledge_key=focus_chunk.get("knowledge_key", ""),
        fact_key=draft["fact_key"],
        fact_summary=draft["focus_text"][:500],
        question_fingerprint=draft["fingerprint"],
        diversity_exhausted=bool(draft["coverage_exhausted"]),
        time=now.isoformat(),
        update_time=now.isoformat(),
        created_at=now,
    ))
    record.current_quiz_json = "{}"

    flow = transition(current, bool(result["is_correct"]))
    completed = flow.completed
    next_type = flow.next_type
    if completed:
        session["quiz_complete"] = True
    else:
        session["quiz_current_type"] = next_type

    db.session.commit()
    redirect_url = (
        url_for("history.history_page")
        if completed
        else url_for("exercise.quiz_page", quiz_type=next_type, autogenerate=1)
    )
    current_app.logger.info(
        "Exercise submitted request=%s type=%s grading_source=%s score=%s total=%.3fs",
        request_id, quiz.get("type"), result.get("grading_source"), result.get("score"),
        time.perf_counter() - submit_started_at,
    )
    return jsonify({
        "ok": True,
        "is_correct": result["is_correct"],
        "score": result["score"],
        "feedback": result["reasoning"],
        "key_point_results": result.get("key_point_results", []),
        "correct_answer": result["correct_answer"],
        "explanation": quiz.get("explanation", ""),
        "citations": draft["citations"],
        "current_type": current,
        "next_type": next_type,
        "completed": completed,
        "redirect": redirect_url,
        "topic": draft["topic"],
        "review": build_review_payload(quiz, student_answer, result),
    })
