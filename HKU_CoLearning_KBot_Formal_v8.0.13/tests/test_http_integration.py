from __future__ import annotations

import json

import requests

from common.models import QuizSession, StudentQARecord, User, db
from student_app.app import create_app as create_student_app
from teacher_app.app import create_app as create_teacher_app


def upload_real_pdf(server, real_pdf):
    response = requests.post(
        server.base_url + "/api/admin/documents",
        headers={"X-Knowledge-Admin-Key": "admin-test"},
        files={"file": (real_pdf.name, real_pdf.read_bytes(), "application/pdf")},
        data={"uploaded_by": "integration-test"},
        timeout=90,
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]


def student_config(server, tmp_path):
    return {
        "TESTING": True,
        "SECRET_KEY": "student-test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'shared.db'}",
        "KNOWLEDGE_SERVICE_URL": server.base_url,
        "KNOWLEDGE_READ_KEY": "read-test",
        "COZE_TOKEN": "",
        "COZE_QUIZ_BOT_ID": "",
        "COZE_GRADING_BOT_ID": "",
        "COZE_NOTES_BOT_ID": "",
    }


def register(client, username="student_test"):
    response = client.post(
        "/register",
        json={
            "username": username,
            "name": "Student Test",
            "password": "secure123",
            "confirm_password": "secure123",
        },
    )
    assert response.status_code == 201, response.get_data(as_text=True)


def correct_answer(quiz):
    if quiz["type"] in {"true_false", "mcq"}:
        return quiz["correct_index"]
    if quiz["type"] == "matching":
        return quiz["correct_mapping"]
    if quiz["type"] == "fill_blank":
        return quiz["answer"]
    return quiz["reference"]


def server_quiz(app, client):
    with client.session_transaction() as browser_session:
        quiz_session_id = browser_session["quiz_session_id"]
    with app.app_context():
        record = db.session.get(QuizSession, quiz_session_id)
        return json.loads(record.current_quiz_json)["quiz"]


def wrong_answer(quiz):
    if quiz["type"] in {"true_false", "mcq"}:
        return (int(quiz["correct_index"]) + 1) % len(quiz["options"])
    if quiz["type"] == "matching":
        mapping = list(quiz["correct_mapping"])
        return mapping[1:] + mapping[:1]
    return "an answer not supported by the notes"


def get_chapter_id(server):
    response = requests.get(
        server.base_url + "/api/chapters",
        headers={"X-Knowledge-Read-Key": "read-test"},
        timeout=10,
    )
    return response.json()["chapters"][0]["id"]


def test_routes_registration_login_sequential_flow_notes_and_teacher_offline(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    config = student_config(knowledge_server, tmp_path)
    student = create_student_app(config)
    client = student.test_client()
    register(client)
    assert client.get("/dashboard").status_code == 200
    chapter_id = get_chapter_id(knowledge_server)
    setup = client.post("/api/exercise/setup", json={"chapter_id": chapter_id})
    assert setup.status_code == 200

    pages = []
    order = ("true_false", "mcq", "matching", "fill_blank", "short_answer")
    for index, quiz_type in enumerate(order):
        body = {"quiz_type": quiz_type}
        if index == 0:
            body["topic"] = "GPS"
        generated = client.post("/api/exercise/generate", json=body)
        assert generated.status_code == 200, generated.get_data(as_text=True)
        payload = generated.get_json()
        assert payload["quiz_type"] == quiz_type
        assert payload["topic"] == "GPS"
        assert "citations" not in payload
        visible_quiz = payload["quiz"]
        for hidden in ("correct_index", "correct_mapping", "answer", "reference", "explanation"):
            assert hidden not in visible_quiz
        full_quiz = server_quiz(student, client)

        if quiz_type == "mcq":
            # The visible progress indicator and edited URL cannot skip steps.
            skipped = client.get("/exercise/short_answer")
            assert skipped.status_code == 302
            assert "/exercise/mcq" in skipped.headers["Location"]
            rejected = client.post("/api/exercise/generate", json={"quiz_type": "matching"})
            assert rejected.status_code == 409

            # An incorrect answer stays on MCQ and requires another MCQ.
            failed = client.post(
                "/api/exercise/submit",
                json={"quiz_id": payload["quiz_id"], "answer": wrong_answer(full_quiz)},
            )
            assert failed.status_code == 200
            failed_data = failed.get_json()
            assert not failed_data["is_correct"]
            assert failed_data["next_type"] == "mcq"
            generated = client.post("/api/exercise/generate", json={"quiz_type": "mcq"})
            assert generated.status_code == 200
            payload = generated.get_json()
            assert payload["topic"] == "GPS"
            assert "citations" not in payload
            full_quiz = server_quiz(student, client)

        submitted = client.post(
            "/api/exercise/submit",
            json={"quiz_id": payload["quiz_id"], "answer": correct_answer(full_quiz)},
        )
        assert submitted.status_code == 200, submitted.get_data(as_text=True)
        result = submitted.get_json()
        assert result["score"] >= 65
        assert result["citations"]
        pages.extend(item["page"] for item in result["citations"] if item.get("page"))
        if quiz_type != "short_answer":
            assert result["next_type"] == order[index + 1]
        else:
            assert result["completed"] is True
            assert result["next_type"] is None

    # No Teacher App/process has been created or started. Student Notes QA and
    # a newly started exercise session still use the independent service.
    client.post("/api/exercise/setup", json={"chapter_id": chapter_id})
    extra = client.post("/api/exercise/generate", json={"quiz_type": "true_false", "topic": "GPS"})
    assert extra.status_code == 200
    notes = client.post("/api/notes/ask", json={"chapter_id": chapter_id, "question": "What is GPS?"})
    assert notes.status_code == 200
    note_data = notes.get_json()
    assert note_data["citations"] and note_data["citations"][0]["source_file"] == real_pdf.name
    assert note_data["citations"][0]["page"]
    assert note_data["citations"][0]["excerpt"]
    assert client.get("/history").status_code == 200
    assert len(set(pages)) > 1

    assert client.post("/logout").status_code == 200
    login = client.post("/login", json={"username": "student_test", "password": "secure123"})
    assert login.status_code == 200


def test_unmatched_first_topic_falls_back_and_stale_unvalidated_topic_is_repaired(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "topic_retry_student")
    chapter_id = get_chapter_id(knowledge_server)
    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200

    invalid = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "linear algebra"},
    )
    assert invalid.status_code == 200, invalid.get_data(as_text=True)
    assert invalid.get_json()["topic"] == "linear algebra"
    assert "whole chapter" in invalid.get_json()["fallback_notice"]
    with client.session_transaction() as browser_session:
        assert browser_session["quiz_topic"] == "linear algebra"

    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    valid = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "GPS"},
    )
    assert valid.status_code == 200, valid.get_data(as_text=True)
    assert valid.get_json()["topic"] == "GPS"

    # Also repair an already-open v8.0.7 browser session that contains the old
    # pre-validation stale topic but no saved question draft.
    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    with client.session_transaction() as browser_session:
        browser_session["quiz_topic"] = "linear algebra"
    repaired = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "LBS"},
    )
    assert repaired.status_code == 200, repaired.get_data(as_text=True)
    assert repaired.get_json()["topic"] == "LBS"


def test_shared_database_teacher_login_registration_and_feedback(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    config = student_config(knowledge_server, tmp_path)
    student = create_student_app(config)
    student_client = student.test_client()
    register(student_client, "shared_student")
    chapter_id = get_chapter_id(knowledge_server)
    student_client.post("/api/exercise/setup", json={"chapter_id": chapter_id})
    generated = student_client.post(
        "/api/exercise/generate", json={"quiz_type": "true_false", "topic": "GPS"}
    ).get_json()
    full_quiz = server_quiz(student, student_client)
    student_client.post(
        "/api/exercise/submit",
        json={"quiz_id": generated["quiz_id"], "answer": full_quiz["correct_index"]},
    )

    teacher = create_teacher_app({
        "TESTING": True,
        "SECRET_KEY": "teacher-test",
        "SQLALCHEMY_DATABASE_URI": config["SQLALCHEMY_DATABASE_URI"],
        "KNOWLEDGE_SERVICE_URL": knowledge_server.base_url,
        "KNOWLEDGE_ADMIN_KEY": "admin-test",
        "SEED_DEMO_TEACHER": False,
        "LEGACY_TEACHER_COMPAT": False,
    })
    with teacher.app_context():
        user = User(username="teacher_test", name="Teacher Test", role="teacher", password_hash="")
        user.set_password("secure123")
        db.session.add(user)
        db.session.commit()
        record_id = StudentQARecord.query.filter_by(student_name="shared_student").one().id

    client = teacher.test_client()
    assert client.get("/register").status_code == 200
    registered = client.post(
        "/api/register",
        json={"username": "teacher_registered", "name": "Registered Teacher", "password": "secure123"},
    )
    assert registered.status_code == 201
    client.post("/api/logout")

    assert client.post("/login", data={"username": "teacher_test", "password": "secure123"}).status_code == 302
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"shared_student" in dashboard.data
    stats = client.get("/dashboard")
    assert stats.status_code == 200
    assert stats.get_json()["total_records"] >= 1
    feedback = client.post(
        f"/records/{record_id}/feedback",
        data={"teacher_score": "95", "teacher_feedback": "Good evidence."},
    )
    assert feedback.get_json()["ok"]
    with student.app_context():
        record = db.session.get(StudentQARecord, record_id)
        assert record.teacher_feedback == "Good evidence."


def test_student_modules_are_independently_deployable(knowledge_server, tmp_path):
    config = student_config(knowledge_server, tmp_path)
    exercise_only = create_student_app(config, enabled_modules={"exercise"})
    notes_only = create_student_app(
        {**config, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'notes.db'}"},
        enabled_modules={"notes"},
    )
    exercise_rules = {rule.rule for rule in exercise_only.url_map.iter_rules()}
    notes_rules = {rule.rule for rule in notes_only.url_map.iter_rules()}
    assert "/api/exercise/generate" in exercise_rules and "/api/notes/ask" not in exercise_rules
    assert "/api/notes/ask" in notes_rules and "/api/exercise/generate" not in notes_rules
