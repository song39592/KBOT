from __future__ import annotations

import datetime as dt
import uuid

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(80), nullable=False, default="")
    role = db.Column(db.String(20), nullable=False, default="student", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash or "", password or "")
        except (ValueError, TypeError):
            return False


class StudentQARecord(db.Model):
    __tablename__ = "student_qa_records"

    id = db.Column(db.String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    student_name = db.Column(db.String(128), nullable=False, index=True)
    chapter = db.Column(db.String(256), nullable=False, default="")
    chapter_id = db.Column(db.String(160), nullable=False, default="", index=True)
    question_type = db.Column(db.String(32), nullable=False, index=True)
    topic = db.Column(db.String(256), nullable=False, default="")
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False, default="")
    options_or_pairs = db.Column(db.Text)
    correct_answer = db.Column(db.Text)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)
    score = db.Column(db.Integer, nullable=False, default=0)
    ai_feedback = db.Column(db.Text)
    teacher_feedback = db.Column(db.Text)
    explanation = db.Column(db.Text)
    generation_source = db.Column(db.String(32), nullable=False, default="local_fallback")
    grading_source = db.Column(db.String(32), nullable=False, default="local")
    source = db.Column(db.String(32), nullable=False, default="local_fallback")  # legacy alias
    citations_json = db.Column(db.Text, nullable=False, default="[]")
    source_document = db.Column(db.String(255), nullable=False, default="")
    source_page = db.Column(db.Integer)
    source_pages_json = db.Column(db.Text, nullable=False, default="[]")
    source_excerpt = db.Column(db.Text)
    source_chunk_id = db.Column(db.String(160), nullable=False, default="", index=True)
    knowledge_key = db.Column(db.String(160), nullable=False, default="", index=True)
    fact_key = db.Column(db.String(80), nullable=False, default="", index=True)
    fact_summary = db.Column(db.String(500), nullable=False, default="")
    question_fingerprint = db.Column(db.String(80), nullable=False, default="", index=True)
    diversity_exhausted = db.Column(db.Boolean, nullable=False, default=False)
    teacher_score = db.Column(db.Integer)
    teacher_verified = db.Column(db.Boolean, nullable=False, default=False)
    verified_by = db.Column(db.String(128), nullable=False, default="")
    verified_at = db.Column(db.String(32), nullable=False, default="")
    time = db.Column(db.String(32), nullable=False, default="")
    update_time = db.Column(db.String(32), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


class NotesQARecord(db.Model):
    __tablename__ = "notes_qa_records"

    id = db.Column(db.String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    student_name = db.Column(db.String(128), nullable=False, index=True)
    chapter_id = db.Column(db.String(160), nullable=False, default="")
    chapter = db.Column(db.String(256), nullable=False, default="")
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    citations_json = db.Column(db.Text, nullable=False, default="[]")
    insufficient_information = db.Column(db.Boolean, nullable=False, default=False)
    answering_source = db.Column(db.String(32), nullable=False, default="local_fallback")
    source = db.Column(db.String(32), nullable=False, default="local_fallback")  # legacy alias
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


class QuizSession(db.Model):
    __tablename__ = "quiz_sessions"

    id = db.Column(db.String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    student_name = db.Column(db.String(128), nullable=False, index=True)
    chapter_id = db.Column(db.String(160), nullable=False, default="")
    used_knowledge_json = db.Column(db.Text, nullable=False, default="[]")  # legacy-compatible
    used_chunks_json = db.Column(db.Text, nullable=False, default="[]")
    used_pages_json = db.Column(db.Text, nullable=False, default="[]")
    used_facts_json = db.Column(db.Text, nullable=False, default="[]")
    used_fingerprints_json = db.Column(db.Text, nullable=False, default="[]")
    current_quiz_json = db.Column(db.Text, nullable=False, default="{}")
    generation_failures_json = db.Column(db.Text, nullable=False, default="{}")
    skipped_types_json = db.Column(db.Text, nullable=False, default="[]")
    exhausted_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class BankQuestion(db.Model):
    """Teacher-authored exercises; never indexed as course knowledge."""
    __tablename__ = "teacher_bank_questions"
    id = db.Column(db.String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    course = db.Column(db.String(160), nullable=False, index=True)
    question_id = db.Column(db.String(80), nullable=False)
    question_type = db.Column(db.String(8), nullable=False, index=True)
    topic = db.Column(db.String(256), nullable=False, default="")
    question = db.Column(db.Text, nullable=False)
    options = db.Column(db.JSON, nullable=False)
    correct_answer = db.Column(db.String(8), nullable=False)
    explanation = db.Column(db.Text, nullable=False, default="")
    source = db.Column(db.String(32), nullable=False, default="teacher_upload")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    details = db.relationship("BankQuestionDetails", uselist=False, back_populates="question", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (db.UniqueConstraint("owner_id", "course", "question_id", name="uq_bank_owner_course_question"),)


class BankQuestionDetails(db.Model):
    """Structured private answer data; a new table avoids altering the existing bank."""
    __tablename__ = "teacher_bank_question_details"
    question_id = db.Column(db.String(32), db.ForeignKey("teacher_bank_questions.id"), primary_key=True)
    data = db.Column(db.JSON, nullable=False)
    question = db.relationship("BankQuestion", back_populates="details")
