from __future__ import annotations

import json

from flask import abort, redirect, url_for, Response, Blueprint, jsonify, render_template, request, session

from common.auth import student_login_required
from common.knowledge_client import KnowledgeServiceError
from common.models import NotesQARecord, NotesDetail, NotesFeedback, NotesCorrection, NotesBookmark, db
from common.notes_engine import answer_notes
from student_app.services import agent_router, knowledge_client, notes_router


bp = Blueprint("notes", __name__)


@bp.get("/notes")
@student_login_required
def notes_page():
    chapters, error = [], ""
    try:
        chapters = knowledge_client().chapters()
    except KnowledgeServiceError:
        error = "Course materials are temporarily unavailable."
    return render_template("notes.html", chapters=chapters, knowledge_error=error)


@bp.post("/api/notes/ask")
@student_login_required
def ask_notes():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "").strip()
    chapter_id = str(data.get("chapter_id") or "").strip()
    topic = " ".join(str(data.get("topic") or question).casefold().split())
    if not 1 <= len(topic) <= 120:
        return jsonify({"ok": False, "error": "Enter a knowledge point (maximum 120 characters)."}), 400
    if not (3 <= len(question) <= 1000) or not chapter_id:
        return jsonify({"ok": False, "error": "Enter a question and select a course chapter."}), 400
    try:
        search = knowledge_client().search(question, chapter_id, 8, {})
    except KnowledgeServiceError:
        return jsonify({"ok": False, "error": "Course materials are temporarily unavailable."}), 503
    result = answer_notes(question, search.get("results", []), notes_router(web_search=data.get("web_search") is True), session["student_username"])
    chapter = result["citations"][0].get("chapter", "") if result["citations"] else ""
    record = NotesQARecord(
        student_name=session["student_username"], chapter_id=chapter_id, chapter=chapter, question=question,
        answer=result["answer"], citations_json=json.dumps(result["citations"], ensure_ascii=False),
        insufficient_information=result["insufficient_information"], answering_source=result["answering_source"], source=result["answering_source"],
    )
    db.session.add(record)
    db.session.flush()
    db.session.add(NotesDetail(record_id=record.id, topic=topic, sections_json=json.dumps(result.get("sections", []))))
    db.session.commit()
    return jsonify({"ok": True, "record_id": record.id, "answering_source": result["answering_source"], "web_results": result.get("web_results", []), "search_status": result.get("search_status", "not_enabled"), "evidence_status": result["evidence_status"], "evidence_message": result["evidence_message"], "missing_information": result["missing_information"], "clarification_question": result["clarification_question"], "sections": result.get("sections", []), "answer": result["answer"], "citations": result["citations"], "insufficient_information": result["insufficient_information"]})



@bp.post("/api/notes/<record_id>/feedback")
@student_login_required
def report_answer(record_id):
    record = NotesQARecord.query.filter_by(id=record_id, student_name=session["student_username"]).first()
    if record is None:
        return jsonify(ok=False, error="Answer not found."), 404
    data = request.get_json(silent=True) or {}
    category = str(data.get("category") or "")
    comment = str(data.get("comment") or "").strip()
    if category not in {"incorrect", "incomplete", "citation", "other"} or not 3 <= len(comment) <= 2000:
        return jsonify(ok=False, error="Choose a category and enter 3–2000 characters."), 400
    if NotesFeedback.query.filter_by(record_id=record_id).first():
        return jsonify(ok=False, error="Feedback has already been submitted for this answer."), 409
    from sqlalchemy.exc import IntegrityError
    db.session.add(NotesFeedback(record_id=record_id, student_name=session["student_username"], category=category, comment=comment))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error="Feedback has already been submitted."), 409
    return jsonify(ok=True)


def own_record(record_id):
    record = NotesQARecord.query.filter_by(id=record_id, student_name=session["student_username"]).first()
    if record is None:
        abort(404)
    return record

@bp.get("/notes/history")
@student_login_required
def notes_history():
    import secrets
    session.setdefault("notes_csrf", secrets.token_urlsafe(32))
    query = NotesQARecord.query.filter_by(student_name=session["student_username"])
    if request.args.get("saved") == "1":
        query = query.join(NotesBookmark, NotesBookmark.record_id == NotesQARecord.id)
    q = request.args.get("q", "").strip()[:200]
    if q:
        query = query.filter(NotesQARecord.question.contains(q, autoescape=True))
    page = max(1, request.args.get("page", 1, type=int))
    total = query.count()
    records = query.order_by(NotesQARecord.created_at.desc()).offset((page-1)*20).limit(20).all()
    rows = []
    for record in records:
        rows.append((record, json.loads(record.citations_json or "[]"), NotesCorrection.query.filter_by(record_id=record.id).order_by(NotesCorrection.created_at).all(), db.session.get(NotesBookmark, record.id) is not None, NotesFeedback.query.filter_by(record_id=record.id).first()))
    return render_template("notes_history.html", rows=rows, page=page, total=total)

@bp.post("/notes/<record_id>/bookmark")
@student_login_required
def bookmark(record_id):
    import hmac
    own_record(record_id)
    expected = session.get("notes_csrf", "")
    if not expected or not hmac.compare_digest(expected, request.form.get("csrf", "")):
        abort(400)
    item = db.session.get(NotesBookmark, record_id)
    if request.form.get("saved") == "1" and item is None:
        db.session.add(NotesBookmark(record_id=record_id, student_name=session["student_username"]))
    elif request.form.get("saved") == "0" and item:
        db.session.delete(item)
    db.session.commit()
    return redirect(url_for("notes.notes_history"))

@bp.get("/notes/<record_id>/reference/<int:index>")
@student_login_required
def reference(record_id, index):
    record = own_record(record_id)
    refs = json.loads(record.citations_json or "[]")
    ref = next((r for r in refs if r.get("reference_id") == index), None)
    if ref is None:
        abort(404)
    document_id = ref.get("document_id")
    if not document_id:
        return render_template("notes_reference.html", ref=ref, before="", hit=ref.get("excerpt", ""), after="", available=False, record_id=record_id, index=index)
    from urllib.parse import quote
    path = "/api/documents/" + quote(document_id, safe="")
    client = knowledge_client()
    try:
        if request.args.get("download") == "1":
            response = client.session.get(client.base_url+path+"/file", headers=client._headers(False), timeout=client.timeout)
            if response.status_code != 200:
                abort(404)
            return Response(response.content, mimetype=response.headers.get("Content-Type", "application/octet-stream"), headers={"Content-Disposition": response.headers.get("Content-Disposition", "attachment"), "X-Content-Type-Options":"nosniff", "Cache-Control":"private, no-store"})
        payload = client._request("GET", path+"/pages")
    except KnowledgeServiceError:
        abort(503, description="Source unavailable or removed; your saved answer is retained.")
    text = next((p["text"] for p in payload["pages"] if p["page"] == ref.get("page")), "")
    excerpt = ref.get("excerpt", "")
    import re
    pattern = r"\s+".join(re.escape(word) for word in excerpt.split())
    match = re.search(pattern, text, re.I) if pattern else None
    before, hit, after = (text[:match.start()], text[match.start():match.end()], text[match.end():]) if match else (text, "", "")
    return render_template("notes_reference.html", ref=ref, before=before, hit=hit, after=after, available=True, record_id=record_id, index=index)
