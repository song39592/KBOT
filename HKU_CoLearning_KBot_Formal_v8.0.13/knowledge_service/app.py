from __future__ import annotations

# Support both recommended module startup (python -m package.run) and
# VS Code/direct-file startup (python package/app.py) on Windows.
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import hmac

from flask import send_file, abort, Flask, jsonify, request
from werkzeug.utils import secure_filename

from common.config import apply_overrides, env, env_bool, env_float, env_int, resolve_path
from knowledge_service.chroma_index import ChromaKnowledgeIndex
from knowledge_service.repository import KnowledgeRepository


BUILD_VERSION = "knowledge-service-8.0.13"


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    config = {
        "KNOWLEDGE_DATA_DIR": str(resolve_path(env("KNOWLEDGE_DATA_DIR", "data/knowledge"))),
        "KNOWLEDGE_ADMIN_KEY": (env("KNOWLEDGE_ADMIN_KEY") or "local-admin-key-change-before-deployment"),
        "KNOWLEDGE_READ_KEY": (env("KNOWLEDGE_READ_KEY") or "local-read-key-change-before-deployment"),
        "MAX_CONTENT_LENGTH": env_int("MAX_UPLOAD_SIZE_MB", 50, 1, 250) * 1024 * 1024,
        "KNOWLEDGE_EMBEDDING_MODEL_PATH": str(resolve_path(env("KNOWLEDGE_EMBEDDING_MODEL_PATH", "models/bge-base-en-v1.5"))),
        "KNOWLEDGE_CHROMA_ENABLED": env_bool("KNOWLEDGE_CHROMA_ENABLED", True),
        "KNOWLEDGE_CHROMA_PATH": str(resolve_path(env("KNOWLEDGE_CHROMA_PATH", "data/knowledge/chroma"))),
        "KNOWLEDGE_CHROMA_COLLECTION": env("KNOWLEDGE_CHROMA_COLLECTION", "course_chunks"),
        "KNOWLEDGE_CHROMA_TOP_K": env_int("KNOWLEDGE_CHROMA_TOP_K", 80, 1, 250),
        "KNOWLEDGE_DENSE_SEARCH_WEIGHT": env_float("KNOWLEDGE_DENSE_SEARCH_WEIGHT", 0.35, 0.0, 0.65),
        "TESTING": False,
    }
    app.config.update(apply_overrides(config, overrides))
    if app.config["TESTING"] and not (overrides or {}).get("KNOWLEDGE_CHROMA_ENABLED"):
        app.config["KNOWLEDGE_CHROMA_ENABLED"] = False
    index = ChromaKnowledgeIndex(
        enabled=app.config["KNOWLEDGE_CHROMA_ENABLED"],
        persist_path=app.config["KNOWLEDGE_CHROMA_PATH"],
        collection_name=app.config["KNOWLEDGE_CHROMA_COLLECTION"],
        model_path=app.config["KNOWLEDGE_EMBEDDING_MODEL_PATH"],
        top_k=app.config["KNOWLEDGE_CHROMA_TOP_K"],
    )
    repository = KnowledgeRepository(
        app.config["KNOWLEDGE_DATA_DIR"],
        semantic_index=index,
        dense_weight=app.config["KNOWLEDGE_DENSE_SEARCH_WEIGHT"],
    )
    app.extensions["knowledge_repository"] = repository
    app.extensions["knowledge_chunk_index"] = index

    def authorized(admin: bool) -> bool:
        expected = app.config["KNOWLEDGE_ADMIN_KEY" if admin else "KNOWLEDGE_READ_KEY"]
        header = "X-Knowledge-Admin-Key" if admin else "X-Knowledge-Read-Key"
        supplied = request.headers.get(header, "")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def deny():
        return jsonify({"ok": False, "error": "Knowledge Service authorization failed."}), 401

    @app.get("/")
    def index():
        status = repository.health()
        return jsonify({
            "ok": True,
            "service": "knowledge",
            "build": BUILD_VERSION,
            "message": "API-only Knowledge Service. Course materials are managed from the Teacher App.",
            "health_url": "/health",
            **status,
        })

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "knowledge", "build": BUILD_VERSION, **repository.health()})

    @app.get("/api/build-info")
    def build_info():
        return jsonify({
            "ok": True,
            "service": "knowledge",
            "build": BUILD_VERSION,
            "storage": "sqlite-metadata+chroma-rrf",
            "retrieval": repository.retrieval_backend_name(),
        })

    @app.get("/api/chapters")
    def chapters():
        if not authorized(False):
            return deny()
        return jsonify({"ok": True, "chapters": repository.chapters()})

    @app.post("/api/search")
    def search():
        if not authorized(False):
            return deny()
        data = request.get_json(silent=True) or {}
        result = repository.search(
            query=str(data.get("query") or ""),
            limit=data.get("limit") or 8,
            chapter_id=str(data.get("chapter_id") or ""),
            used=data.get("used") if isinstance(data.get("used"), dict) else {},
            topic_mode=data.get("topic_mode") is True,
            neighbor_pages=data.get("neighbor_pages") or 0,
        )
        return jsonify({"ok": True, **result, "count": len(result["results"])})

    @app.post("/api/matching-candidates")
    def matching_candidates():
        if not authorized(False):
            return deny()
        data = request.get_json(silent=True) or {}
        chapter_id = str(data.get("chapter_id") or "")
        if not chapter_id:
            return jsonify({"ok": False, "error": "chapter_id is required."}), 400
        items = repository.matching_candidates(str(data.get("query") or ""), chapter_id, data.get("limit") or 12)
        return jsonify({"ok": True, "candidates": items, "count": len(items)})

    @app.get("/api/documents/<document_id>/pages")
    def read_pages(document_id):
        if not authorized(False):
            return deny()
        try:
            repository.get_document(document_id)
        except KeyError:
            abort(404)
        return jsonify(ok=True, pages=repository.pages(document_id))

    @app.get("/api/documents/<document_id>/file")
    def read_file(document_id):
        if not authorized(False):
            return deny()
        try:
            doc = repository.get_document(document_id)
        except KeyError:
            abort(404)
        path = (repository.upload_dir / doc["stored_path"]).resolve()
        if not path.is_relative_to(repository.upload_dir.resolve()) or not path.is_file():
            abort(404)
        return send_file(path, download_name=doc["filename"], as_attachment=path.suffix.lower() != ".pdf")

    @app.get("/api/admin/documents")
    def documents():
        if not authorized(True):
            return deny()
        return jsonify({"ok": True, "documents": repository.documents()})

    @app.get("/api/admin/documents/<document_id>/pages")
    def document_pages(document_id: str):
        if not authorized(True):
            return deny()
        try:
            repository.get_document(document_id)
        except KeyError:
            return jsonify({"ok": False, "error": "Document not found."}), 404
        return jsonify({"ok": True, "pages": repository.pages(document_id)})

    @app.post("/api/admin/documents")
    def upload_document():
        if not authorized(True):
            return deny()
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "A course-material file is required."}), 400
        filename = secure_filename(uploaded.filename)
        if Path(filename).suffix.casefold() not in {".pdf", ".docx", ".txt"}:
            return jsonify({"ok": False, "error": "Only PDF, DOCX, and TXT files are supported."}), 400
        try:
            document = repository.upload(uploaded.read(), filename, str(request.form.get("uploaded_by") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 422
        except Exception:
            app.logger.exception("Knowledge indexing failed")
            return jsonify({"ok": False, "error": "The material could not be indexed."}), 422
        return jsonify({"ok": True, "document": document}), 201

    @app.delete("/api/admin/documents/<document_id>")
    def delete_document(document_id: str):
        if not authorized(True):
            return deny()
        if not repository.delete(document_id):
            return jsonify({"ok": False, "error": "Document not found."}), 404
        return jsonify({"ok": True})

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"ok": False, "error": "The uploaded file exceeds the configured size limit."}), 413

    @app.errorhandler(500)
    def internal_error(_error):
        app.logger.exception("Unhandled Knowledge Service error")
        return jsonify({"ok": False, "error": "The Knowledge Service could not complete the request."}), 500

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(
        host=env("KNOWLEDGE_HOST", "127.0.0.1"),
        port=env_int("KNOWLEDGE_PORT", 5100, 1, 65535),
        debug=False,
    )
