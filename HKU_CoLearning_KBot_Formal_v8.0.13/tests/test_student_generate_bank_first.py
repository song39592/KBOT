from __future__ import annotations

import json

from common.models import BankQuestion, TeacherQuestionBankItem, User, db
from common.question_bank import upsert_items
from student_app.app import create_app as create_student_app
from teacher_app.app import create_app as create_teacher_app

from tests.test_http_integration import (
    get_chapter_id,
    register,
    student_config,
    upload_real_pdf,
)


BANK_QUESTION = "Bank TF: GPS is a positioning technology used by LBS applications."


def _seed_excel_bank_row(app, *, course: str, topic: str, question_id: str = "TF-GPS-1"):
    """Seed a teammate-style BankQuestion (Excel upload table)."""
    with app.app_context():
        teacher = User.query.filter(User.role.in_(["teacher", "admin"])).first()
        if teacher is None:
            teacher = User(username="bank_owner", name="Bank Owner", role="teacher", password_hash="")
            teacher.set_password("bank_owner")
            db.session.add(teacher)
            db.session.commit()
        row = BankQuestion(
            owner_id=teacher.id,
            course=course,
            question_id=question_id,
            question_type="TF",
            topic=topic,
            question=BANK_QUESTION,
            options={},
            correct_answer="TRUE",
            explanation="GPS underpins many LBS positioning scenarios.",
            source="teacher_upload",
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def _seed_legacy_bank_row(app, *, chapter_id: str, topic: str, fingerprint: str = "bank-fp-gps-tf-1"):
    with app.app_context():
        row = TeacherQuestionBankItem(
            chapter="Ch-3 Enabling Technologies",
            chapter_id=chapter_id,
            question_type="true_false",
            topic=topic,
            question=BANK_QUESTION,
            options_or_pairs=json.dumps(
                {"options": ["True", "False"], "correct_index": 0},
                ensure_ascii=False,
            ),
            correct_answer="True",
            explanation="GPS underpins many LBS positioning scenarios.",
            citations_json="[]",
            question_fingerprint=fingerprint,
            created_by="teacher_seed",
            is_active=True,
        )
        db.session.add(row)
        db.session.commit()
        return row.id


def test_empty_topic_serves_excel_bank_first(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "bank_empty_topic")
    chapter_id = get_chapter_id(knowledge_server)
    # Teachers typically enter the chapter label as course.
    _seed_excel_bank_row(student, course="Chapter 3: Enabling Technologies", topic="GPS")

    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post("/api/exercise/generate", json={"quiz_type": "true_false", "topic": ""})
    assert generated.status_code == 200, generated.get_data(as_text=True)
    payload = generated.get_json()
    assert payload["ok"] is True
    assert payload["from_question_bank"] is True
    assert payload["generated_by_ai"] is False
    assert payload["source_label"] == "From question bank"
    assert payload["quiz"]["question"] == BANK_QUESTION
    assert payload["topic"] == "GPS"


def test_matching_topic_prefers_excel_bank(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "bank_topic_match")
    chapter_id = get_chapter_id(knowledge_server)
    _seed_excel_bank_row(
        student,
        course=chapter_id,
        topic="GPS",
        question_id="TF-GPS-2",
    )

    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "GPS"},
    )
    assert generated.status_code == 200, generated.get_data(as_text=True)
    payload = generated.get_json()
    assert payload["from_question_bank"] is True
    assert payload["generated_by_ai"] is False
    assert payload["source_label"] == "From question bank"
    assert payload["quiz"]["question"] == BANK_QUESTION


def test_no_bank_hit_labels_as_ai_generated(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "ai_label_student")
    chapter_id = get_chapter_id(knowledge_server)

    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "GPS"},
    )
    assert generated.status_code == 200, generated.get_data(as_text=True)
    payload = generated.get_json()
    assert payload["from_question_bank"] is False
    assert payload["generated_by_ai"] is True
    assert payload["source_label"] == "Generated by AI"
    assert payload["quiz"]["question"]
    assert payload["quiz"]["question"] != BANK_QUESTION


def test_empty_topic_without_bank_is_allowed_into_generation(knowledge_server, real_pdf, tmp_path):
    """Empty topic must not be rejected; chapter-wide TF should succeed on Ch-3 PDF."""
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "empty_topic_generate")
    chapter_id = get_chapter_id(knowledge_server)

    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post("/api/exercise/generate", json={"quiz_type": "true_false", "topic": ""})
    payload = generated.get_json()
    assert generated.status_code == 200, payload
    assert payload["ok"] is True
    assert payload["from_question_bank"] is False
    assert payload["generated_by_ai"] is True
    assert payload["source_label"] == "Generated by AI"
    assert payload["quiz"]["question"]
    assert payload["topic"].casefold() != "chapter 3: enabling technologies"


def test_chapter_title_as_topic_is_treated_as_open_ended(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "chapter_title_topic")
    chapter_id = get_chapter_id(knowledge_server)
    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post(
        "/api/exercise/generate",
        json={"quiz_type": "true_false", "topic": "Chapter 3: Enabling Technologies"},
    )
    payload = generated.get_json()
    assert generated.status_code == 200, payload
    assert payload["generated_by_ai"] is True
    assert payload["quiz"]["question"]


def test_legacy_json_bank_still_works(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    student = create_student_app(student_config(knowledge_server, tmp_path))
    client = student.test_client()
    register(client, "legacy_bank")
    chapter_id = get_chapter_id(knowledge_server)
    _seed_legacy_bank_row(student, chapter_id=chapter_id, topic="GPS")
    assert client.post("/api/exercise/setup", json={"chapter_id": chapter_id}).status_code == 200
    generated = client.post("/api/exercise/generate", json={"quiz_type": "true_false", "topic": "GPS"})
    assert generated.status_code == 200, generated.get_data(as_text=True)
    payload = generated.get_json()
    assert payload["from_question_bank"] is True
    assert payload["quiz"]["question"] == BANK_QUESTION


def test_upsert_items_writes_teacher_bank(tmp_path):
    student = create_student_app({
        "TESTING": True,
        "SECRET_KEY": "student-test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'bank_upsert.db'}",
        "KNOWLEDGE_SERVICE_URL": "http://127.0.0.1:9",
        "KNOWLEDGE_READ_KEY": "read-test",
        "COZE_TOKEN": "",
        "COZE_QUIZ_BOT_ID": "",
        "COZE_GRADING_BOT_ID": "",
        "COZE_NOTES_BOT_ID": "",
    })
    with student.app_context():
        written = upsert_items(
            [
                {
                    "chapter_id": "ch-1",
                    "question_type": "true_false",
                    "topic": "GPS",
                    "question": BANK_QUESTION,
                    "options_or_pairs": {"options": ["True", "False"], "correct_index": 0},
                    "correct_answer": "True",
                    "question_fingerprint": "upsert-fp-1",
                }
            ],
            uploaded_by="teacher_a",
        )
        assert written == 1
        row = TeacherQuestionBankItem.query.filter_by(question_fingerprint="upsert-fp-1").one()
        assert row.created_by == "teacher_a"
        assert row.chapter_id == "ch-1"
        assert row.is_active is True


def test_teacher_question_bank_items_route(tmp_path, knowledge_server):
    teacher = create_teacher_app({
        "TESTING": True,
        "SECRET_KEY": "teacher-test",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'teacher_bank.db'}",
        "KNOWLEDGE_SERVICE_URL": knowledge_server.base_url,
        "KNOWLEDGE_READ_KEY": "read-test",
        "KNOWLEDGE_ADMIN_KEY": "admin-test",
        "SEED_DEMO_TEACHER": True,
        "COZE_TOKEN": "",
        "TEACHER_CHROMA_ENABLED": False,
    })
    client = teacher.test_client()
    login = client.post("/api/login", json={"username": "teacher1", "password": "123456"})
    assert login.status_code == 200, login.get_data(as_text=True)

    response = client.post(
        "/question_bank/items",
        json={
            "items": [
                {
                    "chapter_id": "ch-route",
                    "question_type": "true_false",
                    "topic": "LBS",
                    "question": BANK_QUESTION,
                    "options_or_pairs": {"options": ["True", "False"], "correct_index": 0},
                    "correct_answer": "True",
                    "question_fingerprint": "route-fp-1",
                }
            ]
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["written"] == 1
    with teacher.app_context():
        assert TeacherQuestionBankItem.query.filter_by(question_fingerprint="route-fp-1").count() == 1
