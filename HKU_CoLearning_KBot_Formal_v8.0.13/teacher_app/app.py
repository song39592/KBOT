from __future__ import annotations

from werkzeug.middleware.proxy_fix import ProxyFix

# Allow both `python -m teacher_app.run` and direct VS Code execution.
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import csv
import datetime as dt
import io
import json
import math
import re
import uuid
from typing import Any, Iterable

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.config import apply_overrides, database_url, env, env_bool, env_float, env_int, resolve_path, service_secret
from common.feedback_builder import build_record_review
from common.knowledge_client import KnowledgeClient, KnowledgeServiceError
from common.models import StudentQARecord, User, db
from common.question_bank import upsert_items
from common.schema_compat import ensure_legacy_schema
from teacher_app.question_bank import bp as question_bank_bp
from teacher_app.chroma_index import ChromaRecordIndex
from teacher_app.kahoot_export import build_kahoot_xlsx
from teacher_app.record_exports import build_records_xlsx
from teacher_app.record_search import hybrid_search_records, search_backend_name

BUILD_VERSION = "teacher-app-8.0.13"


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _safe_record_review(record: StudentQARecord) -> dict[str, Any]:
    try:
        return build_record_review(record)
    except Exception:
        try:
            score = int(record.score or 0)
        except (TypeError, ValueError):
            score = 0
        return {
            "question_type": record.question_type or "Unknown",
            "question": record.question or "",
            "student_answer": record.answer or "",
            "result": "Correct" if bool(record.is_correct) else "Incorrect",
            "score": score,
            "options": [],
            "correct_label": "Correct answer",
            "correct_answer": record.correct_answer or "",
            "submitted_pairs": [],
            "correct_pairs": [],
            "teacher_feedback": record.teacher_feedback or "",
        }


def _options_display(review: dict[str, Any]) -> str:
    options = review.get("options")
    if not isinstance(options, list):
        return ""
    labels = []
    for option in options:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "").strip()
        text_value = str(option.get("text") or "").strip()
        if label and text_value:
            labels.append(f"{label}. {text_value}")
        elif text_value:
            labels.append(text_value)
    return " | ".join(labels)


def _record_dict(record: StudentQARecord) -> dict[str, Any]:
    review = _safe_record_review(record)
    citations = _json_list(record.citations_json)
    rag_cited = []
    for item in citations:
        if not isinstance(item, dict):
            continue
        rag_cited.append({
            "source_file": item.get("source_file") or record.source_document or "",
            "page": item.get("page"),
            "text": item.get("excerpt") or "",
            "section": item.get("section") or item.get("chapter") or record.chapter or "",
        })
    displayed_score = record.teacher_score if record.teacher_score is not None else record.score
    answer_display = str(review.get("student_answer") or record.answer or "")
    correct_answer_display = str(review.get("correct_answer") or record.correct_answer or "")
    return {
        "id": record.id,
        "name": record.student_name,
        "student_name": record.student_name,
        "chapter": record.chapter or "",
        "question_type": record.question_type or "",
        "question": record.question or "",
        "answer": record.answer or "",
        "answer_display": answer_display,
        "correct_answer_display": correct_answer_display,
        "options_display": _options_display(review),
        "input": record.topic or "",
        "ai_score": int(record.score or 0),
        "ai_correct": bool(record.is_correct),
        "score": int(displayed_score or 0),
        "teacher_score": record.teacher_score,
        "teacher_verified": bool(record.teacher_verified),
        "ai_feedback": record.ai_feedback or "",
        "teacher_feedback": record.teacher_feedback or "",
        "correct_answer": record.correct_answer or "",
        "review": review,
        "rag_cited": rag_cited,
        "generation_source": record.generation_source or record.source or "",
        "grading_source": record.grading_source or "",
        "source_document": record.source_document or "",
        "source_page": record.source_page,
        "update_time": record.update_time or "",
    }


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=0, x_host=0, x_port=0, x_prefix=1)
    semantic_model_path = env("TEACHER_SEMANTIC_MODEL_PATH") or env(
        "TEACHER_EMBEDDING_MODEL_PATH",
        "models/bge-base-en-v1.5",
    )
    config = {
        "SECRET_KEY": service_secret("TEACHER_SECRET_KEY"),
        "SESSION_COOKIE_PATH": env("SESSION_COOKIE_PATH", "/"),
        "REMEMBER_COOKIE_PATH": env("REMEMBER_COOKIE_PATH", "/"),
        "REMEMBER_COOKIE_NAME": "kbot_teacher_remember",
        "SESSION_COOKIE_NAME": "kbot_teacher_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": env("SESSION_COOKIE_SAMESITE", "Lax"),
        "SESSION_COOKIE_SECURE": env_bool("SESSION_COOKIE_SECURE", env("KBOT_ENV", "development").casefold() == "production"),
        "SQLALCHEMY_DATABASE_URI": database_url(),
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "KNOWLEDGE_SERVICE_URL": env("KNOWLEDGE_SERVICE_URL", "http://127.0.0.1:5100"),
        "KNOWLEDGE_ADMIN_KEY": env("KNOWLEDGE_ADMIN_KEY") or "local-admin-key-change-before-deployment",
        "KNOWLEDGE_TIMEOUT": env_float("KNOWLEDGE_TIMEOUT", 15, 2, 120),
        "SEED_DEMO_TEACHER": env_bool("SEED_DEMO_TEACHER", False),
        "DEMO_TEACHER_USERNAME": env("DEMO_TEACHER_USERNAME", "teacher1"),
        "DEMO_TEACHER_PASSWORD": env("DEMO_TEACHER_PASSWORD"),
        "DEMO_TEACHER_NAME": env("DEMO_TEACHER_NAME", "Teacher"),
        "ALLOW_TEACHER_REGISTRATION": env_bool("ALLOW_TEACHER_REGISTRATION", True),
        "TEACHER_SEARCH_MAX_RECORDS": env_int("TEACHER_SEARCH_MAX_RECORDS", 5000, 100, 50000),
        "TEACHER_SEARCH_RESULT_LIMIT": env_int("TEACHER_SEARCH_RESULT_LIMIT", 100, 1, 250),
        "TEACHER_SEMANTIC_MODEL_PATH": str(resolve_path(semantic_model_path)) if semantic_model_path else "",
        "TEACHER_DENSE_SEARCH_WEIGHT": env_float("TEACHER_DENSE_SEARCH_WEIGHT", 0.35, 0.0, 0.65),
        "TEACHER_CHROMA_ENABLED": env_bool("TEACHER_CHROMA_ENABLED", True),
        "TEACHER_CHROMA_PATH": str(resolve_path(env("TEACHER_CHROMA_PATH", "data/teacher/chroma"))),
        "TEACHER_CHROMA_COLLECTION": env("TEACHER_CHROMA_COLLECTION", "student_qa_records"),
        "TEACHER_CHROMA_TOP_K": env_int("TEACHER_CHROMA_TOP_K", 50, 1, 250),
        # v6.2 always provided teacher1/123456. Keep that local fallback when
        # an upgraded database has no teacher/admin account, even if an older
        # v8 .env explicitly disabled demo seeding.
        "LEGACY_TEACHER_COMPAT": env_bool("LEGACY_TEACHER_COMPAT", True),
        "TESTING": False,
    }
    app.config.update(apply_overrides(config, overrides))
    app.config["TEACHER_CHROMA_PATH"] = str(resolve_path(app.config["TEACHER_CHROMA_PATH"]))
    if app.config.get("TEACHER_SEMANTIC_MODEL_PATH"):
        app.config["TEACHER_SEMANTIC_MODEL_PATH"] = str(resolve_path(app.config["TEACHER_SEMANTIC_MODEL_PATH"]))
    app.register_blueprint(question_bank_bp)
    db.init_app(app)
    record_index = ChromaRecordIndex(
        enabled=app.config["TEACHER_CHROMA_ENABLED"],
        persist_path=app.config["TEACHER_CHROMA_PATH"],
        collection_name=app.config["TEACHER_CHROMA_COLLECTION"],
        model_path=app.config["TEACHER_SEMANTIC_MODEL_PATH"],
        top_k=app.config["TEACHER_CHROMA_TOP_K"],
    )
    app.extensions["teacher_record_index"] = record_index

    def refresh_teacher_record_index(
        records: Iterable[StudentQARecord] = (),
        deleted_ids: Iterable[str] = (),
        *,
        prune_missing: bool = False,
    ) -> None:
        if not app.config["TEACHER_CHROMA_ENABLED"]:
            return
        try:
            if deleted_ids:
                record_index.delete(deleted_ids)
            if records:
                record_index.sync(records, prune_missing=prune_missing)
        except Exception as exc:  # pragma: no cover - defensive logging only
            app.logger.warning("Teacher Chroma sync failed: %s", exc)

    login = LoginManager(app)
    login.login_view = "login_page"
    login.login_message = "Please login first"

    @login.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    def knowledge_client() -> KnowledgeClient:
        client = app.extensions.get("knowledge_client")
        if client is None:
            client = KnowledgeClient(
                app.config["KNOWLEDGE_SERVICE_URL"],
                admin_key=app.config["KNOWLEDGE_ADMIN_KEY"],
                timeout=app.config["KNOWLEDGE_TIMEOUT"],
            )
            app.extensions["knowledge_client"] = client
        return client

    with app.app_context():
        db.create_all()
        ensure_legacy_schema(db)
        teacher_count = User.query.filter(User.role.in_(["teacher", "admin"])).count()
        should_seed = bool(
            app.config["SEED_DEMO_TEACHER"]
            or (app.config["LEGACY_TEACHER_COMPAT"] and teacher_count == 0)
        )
        if should_seed:
            username = app.config["DEMO_TEACHER_USERNAME"] or "teacher1"
            password = app.config["DEMO_TEACHER_PASSWORD"] or "123456"
            user = User.query.filter_by(username=username).first()
            if user is None:
                user = User(username=username, name=app.config["DEMO_TEACHER_NAME"], role="admin", password_hash="")
                db.session.add(user)
            # Only the local fallback account is normalised. Existing databases
            # with another teacher/admin are left untouched.
            if teacher_count == 0 or app.config["SEED_DEMO_TEACHER"]:
                user.name = app.config["DEMO_TEACHER_NAME"] or username
                user.role = "admin"
                user.set_password(password)
            db.session.commit()

    def authenticate(username: str, password: str) -> User | None:
        user = User.query.filter_by(username=username).first() if username else None
        if user is None or str(user.role or "").casefold() not in {"teacher", "admin"}:
            return None
        return user if user.check_password(password) else None

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard_page"))
        if request.method == "GET":
            return render_template("login.html")
        user = authenticate(str(request.form.get("username") or "").strip(), str(request.form.get("password") or ""))
        if user is None:
            return render_template("login.html", error="Invalid teacher username or password."), 401
        login_user(user, remember=bool(request.form.get("remember")))
        return redirect(url_for("dashboard_page"))

    @app.post("/api/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        user = authenticate(username, password)
        if user is None:
            return jsonify({"status": "error", "message": "Invalid username or password"}), 401
        login_user(user, remember=bool(data.get("remember")))
        return jsonify({"status": "success", "message": f"Welcome back, {user.name or user.username}", "role": user.role})

    @app.route("/register", methods=["GET", "POST"])
    def register_page():
        if not app.config["ALLOW_TEACHER_REGISTRATION"]:
            return redirect(url_for("login_page"))
        if current_user.is_authenticated:
            return redirect(url_for("dashboard_page"))
        if request.method == "GET":
            return render_template("register.html")
        data = request.form
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        name = str(data.get("name") or "").strip()
        result, status = _create_teacher(username, password, name)
        if status >= 400:
            return render_template("register.html", error=result["message"]), status
        return redirect(url_for("dashboard_page"))

    def _create_teacher(username: str, password: str, name: str) -> tuple[dict[str, str], int]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            return {"status": "error", "message": "Username must be 3–40 characters."}, 400
        if not 6 <= len(password) <= 128:
            return {"status": "error", "message": "Password must be at least 6 characters."}, 400
        existing = User.query.filter_by(username=username).first()
        if existing is not None:
            return {"status": "error", "message": f"Username '{username}' already exists"}, 409
        user = User(username=username, name=(name or username)[:80], role="teacher", password_hash="")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return {"status": "success", "message": f"Account created. Welcome, {user.name or user.username}!", "role": "teacher"}, 201

    @app.post("/api/register")
    def api_register():
        if not app.config["ALLOW_TEACHER_REGISTRATION"]:
            return jsonify({"status": "error", "message": "Teacher registration is disabled."}), 403
        data = request.get_json(silent=True) or {}
        result, status = _create_teacher(
            str(data.get("username") or "").strip(),
            str(data.get("password") or ""),
            str(data.get("name") or "").strip(),
        )
        return jsonify(result), status

    @app.post("/logout")
    @login_required
    def logout_action():
        logout_user()
        return redirect(url_for("login_page"))

    @app.post("/api/logout")
    @login_required
    def api_logout():
        logout_user()
        return jsonify({"status": "success", "message": "Logged out successfully"})

    @app.get("/")
    @login_required
    def dashboard_page():
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 15
        query = StudentQARecord.query.order_by(StudentQARecord.created_at.desc())
        total_items = query.count()
        total_pages = max(1, math.ceil(total_items / per_page))
        page = min(page, total_pages)
        records = query.offset((page - 1) * per_page).limit(per_page).all()
        return render_template(
            "dashboard.html",
            items=[_record_dict(record) for record in records],
            page=page,
            total_pages=total_pages,
            total_items=total_items,
            user=current_user,
        )

    @app.get("/dashboard")
    @login_required
    def dashboard_stats():
        records = StudentQARecord.query.all()
        total = len(records)
        chapter_stats: dict[str, dict[str, int]] = {}
        for record in records:
            chapter = record.chapter or "Unknown"
            stats = chapter_stats.setdefault(chapter, {"total": 0, "correct": 0})
            stats["total"] += 1
            stats["correct"] += int(bool(record.is_correct))
        return jsonify({
            "total_records": total,
            "avg_score": round(sum(int(r.score or 0) for r in records) / total, 1) if total else 0,
            "ai_correct_count": sum(bool(r.is_correct) for r in records),
            "teacher_verified_count": sum(bool(r.teacher_verified) for r in records),
            "pending_review": sum(not bool(r.teacher_verified) for r in records),
            "chapter_stats": chapter_stats,
        })

    @app.post("/knowledge/upload")
    @login_required
    def upload_knowledge():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"status": "error", "message": "Please select a file"}), 400
        try:
            document = knowledge_client().upload(uploaded.read(), uploaded.filename, current_user.username)
            return jsonify({
                "status": "success",
                "message": f"'{document.get('filename', uploaded.filename)}' added to the knowledge base — {document.get('chunk_count', 0)} chunks",
                "doc_id": document.get("id"),
            }), 201
        except KnowledgeServiceError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 503

    @app.post("/question_bank/items")
    @login_required
    def question_bank_upsert():
        """Reserved JSON upsert for the teacher question bank.

        Upload UI / CSV import is owned by teammates. Body:
          {"items": [ {chapter_id, question_type, question, ...}, ... ]}
        See ``common.question_bank`` module docstring for the full field schema.
        """
        data = request.get_json(silent=True) or {}
        items = data.get("items")
        if items is None and isinstance(data, list):
            items = data
        if not isinstance(items, list) or not items:
            return jsonify({
                "ok": False,
                "status": "error",
                "message": "Provide a non-empty JSON list under 'items'.",
            }), 400
        try:
            written = upsert_items(items, uploaded_by=current_user.username)
        except ValueError as exc:
            return jsonify({"ok": False, "status": "error", "message": str(exc)}), 400
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify({
                "ok": False,
                "status": "error",
                "message": "Could not save question bank items.",
            }), 500
        return jsonify({
            "ok": True,
            "status": "success",
            "written": written,
            "message": f"Upserted {written} question bank item(s).",
        }), 200

    @app.get("/knowledge/list")
    @login_required
    def list_knowledge():
        try:
            documents = knowledge_client().documents()
        except KnowledgeServiceError:
            return jsonify([])
        return jsonify([{
            "doc_id": item.get("id"),
            "source_file": item.get("filename"),
            "chunk_count": item.get("chunk_count", 0),
            "chapter_count": item.get("chapter_count") or item.get("section_count") or 1,
            "page_count": item.get("page_count", 0),
        } for item in documents])

    @app.post("/knowledge/delete")
    @login_required
    def delete_knowledge_legacy():
        data = request.get_json(silent=True) or {}
        document_id = str(data.get("doc_id") or "").strip()
        if not document_id:
            return jsonify({"status": "error", "message": "doc_id is required"}), 400
        try:
            knowledge_client().delete(document_id)
            return jsonify({"status": "success", "message": "Course material deleted."})
        except KnowledgeServiceError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 503

    @app.post("/knowledge/delete/<document_id>")
    @login_required
    def delete_knowledge(document_id: str):
        try:
            knowledge_client().delete(document_id)
            return jsonify({"ok": True})
        except KnowledgeServiceError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    @app.post("/sync_from_students")
    @login_required
    def sync_from_students():
        return jsonify({"status": "success", "message": "Teacher and student apps already use the same database.", "synced": 0})

    @app.post("/search")
    @login_required
    def search_records():
        keyword = str((request.get_json(silent=True) or {}).get("keyword") or "").strip()[:500]
        if not keyword:
            return jsonify([])

        # v8.0.7-v8.0.11 had regressed to SQL ILIKE only even though the UI
        # still advertised semantic search.  Rank a bounded recent corpus with
        # the restored hybrid keyword + vector search.  Exact names and literal
        # phrases receive strong boosts, while TF-IDF/BM25/character vectors
        # recover paraphrases, acronym/long-form variants and minor spelling
        # differences.  An explicitly configured local SentenceTransformer
        # model can add dense semantic similarity without any request-time
        # download.
        records = (
            StudentQARecord.query
            .order_by(StudentQARecord.created_at.desc())
            .limit(app.config["TEACHER_SEARCH_MAX_RECORDS"])
            .all()
        )
        semantic_scores = record_index.search(
            records,
            keyword,
            top_k=app.config["TEACHER_CHROMA_TOP_K"],
        )
        if semantic_scores:
            records_by_id = {record.id: record for record in records}
            missing_ids = [record_id for record_id in semantic_scores if record_id not in records_by_id]
            if missing_ids:
                for record in StudentQARecord.query.filter(StudentQARecord.id.in_(missing_ids)).all():
                    if record.id not in records_by_id:
                        records.append(record)
                        records_by_id[record.id] = record
        fallback_backend = search_backend_name(app.config["TEACHER_SEMANTIC_MODEL_PATH"])
        ranked = hybrid_search_records(
            records,
            keyword,
            limit=app.config["TEACHER_SEARCH_RESULT_LIMIT"],
            dense_model_path="" if semantic_scores is not None else app.config["TEACHER_SEMANTIC_MODEL_PATH"],
            dense_weight=app.config["TEACHER_DENSE_SEARCH_WEIGHT"],
            semantic_scores=semantic_scores,
            semantic_backend=record_index.backend_name(fallback=fallback_backend),
        )
        output = []
        for record, metadata in ranked:
            item = _record_dict(record)
            item.update(metadata)
            output.append(item)
        return jsonify(output)

    @app.post("/search_by_student_name")
    @login_required
    def search_by_student_name():
        name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
        if not name:
            return jsonify([])
        records = StudentQARecord.query.filter(StudentQARecord.student_name.ilike(f"%{name}%")).order_by(StudentQARecord.created_at.desc()).limit(100).all()
        return jsonify([_record_dict(record) for record in records])

    @app.get("/get_detail/<record_id>")
    @login_required
    def get_detail(record_id: str):
        record = db.session.get(StudentQARecord, record_id)
        if record is None:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        return jsonify({"status": "success", **_record_dict(record)})

    @app.post("/update")
    @login_required
    def update_record():
        data = request.get_json(silent=True) or {}
        record = db.session.get(StudentQARecord, str(data.get("id") or ""))
        if record is None:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        if "score" in data and str(data.get("score") or "").strip():
            try:
                score = int(float(data["score"]))
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "Score must be 0-100"}), 400
            if not 0 <= score <= 100:
                return jsonify({"status": "error", "message": "Score must be 0-100"}), 400
            record.teacher_score = score
        if "teacher_feedback" in data:
            record.teacher_feedback = str(data.get("teacher_feedback") or "").strip()
        if "chapter" in data:
            record.chapter = str(data.get("chapter") or "")[:256]
        if "correct_answer" in data:
            record.correct_answer = str(data.get("correct_answer") or "")
        record.teacher_verified = True
        record.verified_by = current_user.username
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).isoformat()
        record.verified_at = now
        record.update_time = now
        db.session.commit()
        refresh_teacher_record_index([record])
        return jsonify({"status": "success", "message": "Saved", "update_time": now, "ok": True})

    @app.post("/records/<record_id>/feedback")
    @login_required
    def save_feedback(record_id: str):
        record = db.session.get(StudentQARecord, record_id)
        if record is None:
            return jsonify({"ok": False, "error": "Record not found."}), 404
        raw_score = str(request.form.get("teacher_score") or "").strip()
        if raw_score:
            try:
                record.teacher_score = max(0, min(100, int(raw_score)))
            except ValueError:
                return jsonify({"ok": False, "error": "Teacher score must be 0-100."}), 400
        record.teacher_feedback = str(request.form.get("teacher_feedback") or "").strip()
        record.teacher_verified = True
        record.verified_by = current_user.username
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).isoformat()
        record.verified_at = record.update_time = now
        db.session.commit()
        refresh_teacher_record_index([record])
        return jsonify({"ok": True})

    @app.post("/batch_update")
    @login_required
    def batch_update():
        data = request.get_json(silent=True) or {}
        ids = [str(item) for item in data.get("ids", [])]
        updates = data.get("updates") or {}
        records = StudentQARecord.query.filter(StudentQARecord.id.in_(ids)).all() if ids else []
        for record in records:
            if "teacher_verified" in updates:
                record.teacher_verified = bool(updates["teacher_verified"])
            record.verified_by = current_user.username
            record.verified_at = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0).isoformat()
        db.session.commit()
        refresh_teacher_record_index(records)
        return jsonify({"status": "success", "message": f"Updated {len(records)} records"})

    @app.post("/delete")
    @login_required
    def delete_record():
        record = db.session.get(StudentQARecord, str((request.get_json(silent=True) or {}).get("id") or ""))
        if record is None:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        deleted_id = record.id
        db.session.delete(record)
        db.session.commit()
        refresh_teacher_record_index(deleted_ids=[deleted_id])
        return jsonify({"status": "success", "message": "Record deleted"})

    @app.post("/batch_delete")
    @login_required
    def batch_delete():
        ids = [str(item) for item in (request.get_json(silent=True) or {}).get("ids", [])]
        records = StudentQARecord.query.filter(StudentQARecord.id.in_(ids)).all() if ids else []
        deleted_ids = [record.id for record in records]
        for record in records:
            db.session.delete(record)
        db.session.commit()
        refresh_teacher_record_index(deleted_ids=deleted_ids)
        return jsonify({"status": "success", "message": f"Deleted {len(records)} records"})

    @app.get("/student_profile/<path:name>")
    @login_required
    def student_profile(name: str):
        records = StudentQARecord.query.filter_by(student_name=name).all()
        count = len(records)
        return jsonify({
            "name": name,
            "count": count,
            "avg": round(sum(int(r.score or 0) for r in records) / count, 1) if count else 0,
            "ai_correct_rate": round(100 * sum(bool(r.is_correct) for r in records) / count, 1) if count else 0,
            "ai_accuracy": round(100 * sum(bool(r.is_correct) for r in records) / count, 1) if count else 0,
        })

    @app.post("/ai_reevaluate")
    @login_required
    def ai_reevaluate():
        record = db.session.get(StudentQARecord, str((request.get_json(silent=True) or {}).get("id") or ""))
        if record is None:
            return jsonify({"status": "error", "message": "Record not found"}), 404
        # Existing answer remains authoritative; new semantic grading is performed
        # when the student submits Short Answer through the dedicated Grading Agent.
        return jsonify({"status": "success", "message": "The stored AI result is already the shared grading result."})

    def _csv_response(filename: str, kahoot: bool = False) -> Response:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        records = StudentQARecord.query.order_by(StudentQARecord.created_at.desc()).all()
        if kahoot:
            writer.writerow(["Question", "Answer 1", "Answer 2", "Answer 3", "Answer 4", "Time limit", "Correct answer(s)"])
            for record in records:
                options = []
                try:
                    payload = json.loads(record.options_or_pairs or "{}")
                    options = payload.get("options", []) if isinstance(payload, dict) else []
                except (TypeError, json.JSONDecodeError):
                    pass
                options = [str(item) for item in options][:4] + [""] * (4 - len(options))
                writer.writerow([record.question, *options, 30, record.correct_answer or ""])
        else:
            writer.writerow(["student", "chapter", "type", "topic", "question", "answer", "ai_score", "teacher_score", "ai_feedback", "teacher_feedback", "generation_source", "grading_source", "source_pages", "created_at"])
            for record in records:
                writer.writerow([record.student_name, record.chapter, record.question_type, record.topic, record.question, record.answer, record.score, record.teacher_score if record.teacher_score is not None else "", record.ai_feedback or "", record.teacher_feedback or "", record.generation_source, record.grading_source, record.source_pages_json, record.created_at.isoformat() if record.created_at else ""])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

    @app.get("/records/export.csv")
    @app.get("/export")
    @login_required
    def export_records():
        return _csv_response("student_records.csv")

    @app.get("/export_excel")
    @login_required
    def export_excel():
        try:
            records = (
                StudentQARecord.query
                .order_by(StudentQARecord.created_at.desc())
                .all()
            )
            output, record_count = build_records_xlsx(records)
            response = send_file(
                output,
                as_attachment=True,
                download_name="student_records.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            response.headers["X-KBot-Record-Count"] = str(record_count)
            return response
        except Exception:
            app.logger.exception("Excel export failed")
            return jsonify({
                "status": "error",
                "message": "The Excel export could not be created. Check that openpyxl is installed, then restart the Teacher App.",
            }), 422

    @app.get("/export_kahoot")
    @login_required
    def export_kahoot():
        template_path = Path(__file__).with_name("kahoot_template.xlsx")
        try:
            records = (
                StudentQARecord.query
                .order_by(StudentQARecord.created_at.desc())
                .limit(1000)
                .all()
            )
            output, question_count = build_kahoot_xlsx(records, template_path)
            response = send_file(
                output,
                as_attachment=True,
                download_name="kahoot_quiz_template.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            response.headers["X-KBot-Kahoot-Question-Count"] = str(question_count)
            return response
        except Exception:
            # Export errors are logged for the teacher, but never exposed as a
            # generic Flask 500 page. The bundled official template remains a
            # useful safe fallback even when a legacy record is malformed.
            app.logger.exception("Kahoot XLSX export failed; serving blank template")
            if template_path.exists():
                return send_file(
                    template_path,
                    as_attachment=True,
                    download_name="kahoot_quiz_template_blank.xlsx",
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            return jsonify({
                "status": "error",
                "message": "The Kahoot template could not be created. Check that openpyxl is installed, then restart the Teacher App.",
            }), 422

    @app.post("/upload")
    @login_required
    def upload_student_csv():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"status": "error", "message": "Please select a CSV file"}), 400
        try:
            rows = list(csv.DictReader(io.StringIO(uploaded.read().decode("utf-8-sig"))))
        except (UnicodeDecodeError, csv.Error):
            return jsonify({"status": "error", "message": "The CSV could not be read."}), 400
        imported = 0
        imported_records: list[StudentQARecord] = []
        for row in rows[:1000]:
            question = str(row.get("question") or "").strip()
            answer = str(row.get("answer") or "").strip()
            if not question or not answer:
                continue
            score_raw = row.get("score") or 0
            try:
                score = max(0, min(100, int(float(score_raw))))
            except (TypeError, ValueError):
                score = 0
            record = StudentQARecord(
                id=uuid.uuid4().hex,
                student_name=str(row.get("student name") or row.get("student_name") or "Imported Student")[:128],
                chapter=str(row.get("chapter") or "")[:256],
                question_type=str(row.get("question type") or row.get("question_type") or "short_answer")[:32],
                topic=str(row.get("input") or row.get("topic") or "")[:256],
                question=question,
                answer=answer,
                score=score,
                is_correct=score >= 60,
                ai_feedback="Imported from teacher CSV; no new Agent call was made.",
                generation_source="teacher_import",
                grading_source="imported_score",
                source="teacher_import",
            )
            db.session.add(record)
            imported_records.append(record)
            imported += 1
        db.session.commit()
        refresh_teacher_record_index(imported_records)
        return jsonify({"status": "success", "message": f"Imported {imported} student answers."})

    @app.get("/health")
    def health():
        fallback_backend = search_backend_name(app.config.get("TEACHER_SEMANTIC_MODEL_PATH", ""))
        total_records = StudentQARecord.query.count()
        status = {
            "ok": True,
            "service": "teacher",
            "build": BUILD_VERSION,
            "database": False,
            "knowledge": False,
            "record_search": record_index.backend_name(fallback=fallback_backend),
            "record_index": record_index.status(total_records=total_records),
        }
        try:
            db.session.execute(text("SELECT 1"))
            status["database"] = True
        except SQLAlchemyError:
            status["ok"] = False
        try:
            knowledge_client().health()
            status["knowledge"] = True
        except KnowledgeServiceError:
            status["ok"] = False
        return jsonify(status), 200 if status["ok"] else 503

    @app.get("/api/build-info")
    def build_info():
        fallback_backend = search_backend_name(app.config.get("TEACHER_SEMANTIC_MODEL_PATH", ""))
        total_records = StudentQARecord.query.count()
        return jsonify({
            "ok": True,
            "service": "teacher",
            "build": BUILD_VERSION,
            "ui": "v6.2-hku-moodle-restored",
            "knowledge_management": "external-service",
            "record_search": record_index.backend_name(fallback=fallback_backend),
            "record_index": record_index.status(total_records=total_records),
        })

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=env("TEACHER_HOST", "127.0.0.1"), port=env_int("TEACHER_PORT", 5000, 1, 65535), debug=False)
