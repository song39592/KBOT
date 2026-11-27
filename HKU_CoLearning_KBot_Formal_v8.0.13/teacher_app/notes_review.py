from datetime import timedelta
from flask import Blueprint, abort, render_template, request, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy import func
from common.models import db, NotesQARecord, NotesDetail, NotesFeedback, NotesCorrection, utcnow

bp = Blueprint("notes_review", __name__)

@bp.before_request
@login_required
def teacher_only():
    if current_user.role not in {"teacher", "admin"}:
        abort(403)

@bp.get("/notes-review")
def review():
    from flask import session
    import secrets
    session.setdefault("notes_review_csrf", secrets.token_urlsafe(32))
    days = request.args.get("days", "30")
    days = int(days) if days in {"7", "30", "90"} else 30
    cutoff = utcnow() - timedelta(days=days)
    chapter = request.args.get("chapter", "")
    q = request.args.get("q", "").strip()[:120]
    status = request.args.get("status", "open")
    query = db.session.query(NotesDetail.topic, func.count(NotesDetail.record_id).label("total"), func.count(func.distinct(NotesQARecord.student_name)).label("students")).join(NotesQARecord, NotesDetail.record_id == NotesQARecord.id).filter(NotesQARecord.created_at >= cutoff)
    feedback = db.session.query(NotesFeedback, NotesQARecord).join(NotesQARecord, NotesFeedback.record_id == NotesQARecord.id).filter(NotesFeedback.created_at >= cutoff)
    if chapter:
        query = query.filter(NotesQARecord.chapter_id == chapter)
        feedback = feedback.filter(NotesQARecord.chapter_id == chapter)
    if q:
        query = query.filter(NotesDetail.topic.contains(q, autoescape=True))
        feedback = feedback.filter(NotesQARecord.question.contains(q, autoescape=True) | NotesFeedback.comment.contains(q, autoescape=True))
    if status in {"open", "resolved"}:
        feedback = feedback.filter(NotesFeedback.resolved == (status == "resolved"))
    top = query.group_by(NotesDetail.topic).order_by(func.count(NotesDetail.record_id).desc(), NotesDetail.topic).limit(10).all()
    page = max(1, request.args.get("page", 1, type=int))
    total = feedback.count()
    rows = feedback.order_by(NotesFeedback.created_at.desc(), NotesFeedback.id).offset((page-1)*25).limit(25).all()
    chapters = db.session.query(NotesQARecord.chapter_id, NotesQARecord.chapter).distinct().all()
    return render_template("notes_review.html", top=top, rows=rows, replies={r.id: NotesCorrection.query.filter_by(record_id=r.id).order_by(NotesCorrection.created_at).all() for f,r in rows}, chapters=chapters, days=days, chapter=chapter, q=q, status=status, page=page, total=total)

@bp.post("/notes-review/<feedback_id>/resolve")
def resolve(feedback_id):
    # Session-bound token prevents cross-site resolution requests.
    from flask import session
    import hmac
    expected = session.get("notes_review_csrf", "")
    if not expected or not hmac.compare_digest(expected, request.form.get("csrf", "")):
        abort(400)
    item = db.session.get(NotesFeedback, feedback_id)
    if item is None:
        abort(404)
    correction = request.form.get("correction", "").strip()
    if not 3 <= len(correction) <= 10000:
        abort(400, description="Enter a teacher explanation (3–10000 characters).")
    db.session.add(NotesCorrection(record_id=item.record_id, teacher_name=current_user.username, text=correction))
    item.resolved = True
    item.reviewed_by = current_user.username
    db.session.commit()
    return redirect(url_for("notes_review.review"))
