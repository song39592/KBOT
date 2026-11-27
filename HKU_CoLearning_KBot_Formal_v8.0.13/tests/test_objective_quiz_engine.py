"""Objective quiz acceptance: retrieval, teaching feedback and question quality."""
import pytest
from common.models import BankQuestion, BankQuestionDetails, TeacherQuestionBankItem, User, db
from common.question_bank import pick_question
from common.grading_engine import grade_objective
from common.quiz_engine import FocusFact, QuizValidationError, generation_prompt, validate_grounding, _usable_fact
from student_app.app import create_app
from student_app.blueprints.exercise import _generation_response, _draft_from_bank_hit
from tests.test_student_generate_bank_first import _seed_excel_bank_row, _seed_legacy_bank_row


@pytest.fixture
def bank_app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'quiz.db'}"})


@pytest.mark.parametrize("query,match", [("E-COMMERCE", "exact"), ("Ecommerce", "normalized"),
                                        ("e commerce", "normalized"), ("ＥＣＯＭＭＥＲＣＥ", "normalized"),
                                        ("ecomerce", "nearest")])
def test_case_punctuation_and_spelling(bank_app, query, match):
    _seed_excel_bank_row(bank_app, course="ch1", topic="E-commerce", question_id="commerce")
    _seed_excel_bank_row(bank_app, course="ch1", topic="GPS", question_id="gps")
    with bank_app.app_context():
        hit = pick_question("ch1", "true_false", query)
        assert hit["topic"] == "E-commerce"
        assert hit["topic_match"] == match


def test_nearest_is_unused_same_chapter_and_type(bank_app):
    _seed_excel_bank_row(bank_app, course="ch1", topic="E-commerce", question_id="commerce")
    _seed_excel_bank_row(bank_app, course="ch1", topic="GPS", question_id="gps")
    _seed_excel_bank_row(bank_app, course="ch2", topic="ecomerce", question_id="other")
    with bank_app.app_context():
        first = pick_question("ch1", "true_false", "ecomerce")
        second = pick_question("ch1", "true_false", "ecomerce", [first["fingerprint"]])
        assert second["topic"] == "GPS"
        assert pick_question("ch1", "mcq", "ecomerce") is None
        assert pick_question("ch1", "true_false", "", [first["fingerprint"], second["fingerprint"]]) is None


def test_exact_legacy_beats_fuzzy_excel(bank_app):
    _seed_excel_bank_row(bank_app, course="ch1", topic="Economics", question_id="near")
    _seed_legacy_bank_row(bank_app, chapter_id="ch1", topic="E-commerce")
    with bank_app.app_context():
        assert pick_question("ch1", "true_false", "E-commerce")["generation_source"] == "question_bank"


def test_no_two_hundred_row_cutoff(bank_app):
    _seed_excel_bank_row(bank_app, course="ch1", topic="E-commerce", question_id="old")
    with bank_app.app_context():
        owner = User.query.first()
        for index in range(201):
            db.session.add(BankQuestion(owner_id=owner.id, course="ch1", question_id=f"new-{index}",
                question_type="TF", topic="GPS", question="GPS uses satellites.", options={}, correct_answer="TRUE"))
        db.session.commit()
        assert pick_question("ch1", "true_false", "E-commerce")["topic"] == "E-commerce"


def test_matching_uploaded_bank_round_trip(bank_app):
    _seed_excel_bank_row(bank_app, course="ch1", topic="GPS", question_id="seed")
    with bank_app.app_context():
        owner = User.query.first()
        row = BankQuestion(owner_id=owner.id, course="ch1", question_id="MATCH-1", question_type="MATCH",
                           topic="E-commerce", question="Match each business model to its participants.", options={},
                           correct_answer="", explanation="B2B connects businesses; B2C connects a business to consumers.")
        row.details = BankQuestionDetails(data={"schema_version": 1, "pairs": [
            {"left_id": "l1", "left_text": "B2B", "right_id": "r1", "right_text": "Business to business"},
            {"left_id": "l2", "left_text": "B2C", "right_id": "r2", "right_text": "Business to consumer"}],
            "correct_pairs": {"l1": "r1", "l2": "r2"}})
        db.session.add(row)
        db.session.commit()
        hit = pick_question("ch1", "matching", "ecomerce")
        quiz = hit["quiz"]
        assert grade_objective(quiz, quiz["correct_mapping"])["score"] == 100
        wrong = [1 - value for value in quiz["correct_mapping"]]
        feedback = grade_objective(quiz, wrong)
        assert feedback["score"] == 0
        assert all(not item["is_correct"] for item in feedback["pair_feedback"])
        assert "Business to business" in feedback["reasoning"]
        public = _generation_response(_draft_from_bank_hit(hit, "matching", "ecomerce"), "matching", "ecomerce")
        assert "closest question" in public["match_notice"]
        assert not {"correct_mapping", "explanation", "citations"} & public["quiz"].keys()
        assert public["from_question_bank"] and not public["generated_by_ai"]
        assert _generation_response({"quiz_id": "q", "quiz": quiz}, "matching", "")["source_label"].startswith("AI 生成")


@pytest.mark.parametrize("kind", ["mcq", "true_false"])
def test_wrong_choice_feedback_uses_selected_answer_and_explanation(kind):
    quiz = {"type": kind, "question": "GPS requires a receiver only.", "options": ["True", "False"],
            "correct_index": 0, "explanation": "GPS reception does not require a transmitter at the user."}
    result = grade_objective(quiz, 1)
    assert "You selected False" in result["reasoning"]
    assert quiz["explanation"] in result["reasoning"]
    assert grade_objective(quiz, 0)["score"] == 100
    assert grade_objective(quiz, "not an answer")["score"] == 0


@pytest.mark.parametrize("kind", ["mcq", "true_false", "matching"])
def test_prompt_and_validation_reject_chapter_meta_questions(kind):
    fact = "GPS requires a receiver only."
    chunk = {"chunk_id": "c1", "text": fact, "facts": [{"text": fact, "fact_key": "f1"}]}
    focus = FocusFact(fact, "f1", chunk)
    prompt = generation_prompt(kind, "GPS", focus, [chunk], [], {})
    assert "AUTHORING PROCESS" in prompt
    assert "Never ask whether this chapter/course is relevant" in prompt
    with pytest.raises(QuizValidationError, match="chapter relevance"):
        validate_grounding({"type": kind, "question": "This chapter is relevant to E-commerce?"}, [chunk], [], focus)
    assert not _usable_fact("This chapter is relevant to the economic development of commerce.")


def test_empty_keyword_samples_bank(bank_app):
    _seed_excel_bank_row(bank_app, course="ch1", topic="GPS")
    with bank_app.app_context():
        assert pick_question("ch1", "true_false")["topic_match"] == "chapter"


@pytest.mark.parametrize("topic", ["", "ecomerce"])
def test_objective_http_flow_bank_first_feedback_and_draft_reuse(bank_app, monkeypatch, topic):
    from types import SimpleNamespace
    from student_app.blueprints import exercise
    from tests.test_http_integration import register
    monkeypatch.setattr(exercise, "knowledge_client", lambda: SimpleNamespace(
        chapters=lambda: [{"id": "ch1", "label": "Commerce"}]))
    _seed_excel_bank_row(bank_app, course="ch1", topic="E-commerce", question_id="tf")
    with bank_app.app_context():
        owner = User.query.first()
        db.session.add(BankQuestion(owner_id=owner.id, course="ch1", question_id="mcq", question_type="MCQ",
            topic="E-commerce", question="Which model connects businesses?", options={"A": "B2B", "B": "B2C"},
            correct_answer="A", explanation="B2B connects businesses; B2C serves consumers."))
        row = BankQuestion(owner_id=owner.id, course="ch1", question_id="match", question_type="MATCH",
            topic="E-commerce", question="Match models to participants.", options={}, correct_answer="", explanation="Model participants differ.")
        row.details = BankQuestionDetails(data={"pairs": [
            {"left_id": "l1", "left_text": "B2B", "right_id": "r1", "right_text": "Business to business"},
            {"left_id": "l2", "left_text": "B2C", "right_id": "r2", "right_text": "Business to consumer"}],
            "correct_pairs": {"l1": "r1", "l2": "r2"}})
        db.session.add(row)
        db.session.commit()
    client = bank_app.test_client()
    register(client, "objective_flow")
    assert client.post("/api/exercise/setup", json={"chapter_id": "ch1"}).status_code == 200
    for kind in ("true_false", "mcq", "matching"):
        response = client.post("/api/exercise/generate", json={"quiz_type": kind, "topic": topic})
        assert response.status_code == 200, response.get_json()
        data = response.get_json()
        assert data["from_question_bank"]
        assert not {"correct_index", "correct_mapping", "explanation"} & data["quiz"].keys()
        reused = client.post("/api/exercise/generate", json={"quiz_type": kind, "topic": topic}).get_json()
        assert reused["quiz_id"] == data["quiz_id"]
        if topic:
            assert "closest question" in reused["match_notice"]
        answer = [0, 0] if kind == "matching" else 0
        result = client.post("/api/exercise/submit", json={"quiz_id": data["quiz_id"], "answer": answer})
        assert result.status_code == 200, result.get_json()
        graded = result.get_json()
        if kind == "mcq":
            assert graded["is_correct"]
            assert graded["next_type"] == "matching"
            assert "B2B connects businesses" in graded["feedback"]
        if kind == "matching":
            assert len(graded["pair_feedback"]) == 2
            assert graded["score"] == 50


def test_bank_keeps_known_ai_origin_label():
    public = _generation_response({"quiz_id": "q", "quiz": {"type": "mcq", "question": "Q?", "options": ["one", "two"], "correct_index": 0},
        "from_question_bank": True, "generation_source": "coze_quiz_agent"}, "mcq", "")
    assert public["from_question_bank"] and public["generated_by_ai"]
    assert public["source_label"] == "AI 生成 · From question bank"
