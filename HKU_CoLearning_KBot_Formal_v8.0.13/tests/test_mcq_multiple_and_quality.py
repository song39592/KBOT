from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook, load_workbook

from common.mcq_answers import answer_letters, correct_indices
from common.question_bank import HEADERS, parse_workbook, pick_question, quiz_from_bank_question
from common.grading_engine import grade_objective
from common.feedback_builder import build_record_review, build_review_payload, build_detailed_feedback
from common.models import BankQuestion, BankQuestionDetails, StudentQARecord, User, db
from common.quiz_engine import FocusFact, QuizUnavailableError, QuizValidationError, _matching_term, local_quiz, validate_grounding
from common.quiz_parser import normalize_quiz
from student_app.app import create_app as student_app
from teacher_app.app import create_app as teacher_app
from teacher_app.question_bank import explanation_warnings
from teacher_app.kahoot_export import eligible_questions
from tests.test_http_integration import register

MULTI = ['WEB1', 'MCQ', 'Web', 'Which protocols transfer web content? Select all correct answers.',
         'HTTP', 'SMTP', 'HTTPS', 'DNS', '', 'A,C', 'HTTP and HTTPS transfer web content. SMTP sends email; DNS resolves names.']
TF = ['TF1', 'TF', 'Web', 'HTTP transfers web content.', '', '', '', '', '', 'TRUE', 'HTTP transfers web content.']


def workbook(rows):
    book = Workbook()
    book.active.title = 'Questions'
    book.active.append(HEADERS)
    for row in rows:
        book.active.append(row)
    result = io.BytesIO()
    book.save(result)
    book.close()
    return result.getvalue()


@pytest.mark.parametrize('value', ['A,C', 'c,a', ' A ; C ', 'AC', 'A | C', 'A C'])
def test_multiple_answer_notations_are_canonical_and_fit_mysql(value):
    row = MULTI.copy()
    row[9] = value
    questions, errors = parse_workbook(workbook([row]))
    assert not errors
    assert questions[0]['correct_answer'] == 'AC'
    assert len(questions[0]['correct_answer']) <= 8


@pytest.mark.parametrize('value', ['A,A', 'A,A,C', 'A,F', 'A,E', '', 'A/ C', '=A,C', 'all', '1,3'])
def test_invalid_multi_answer_keys_are_rejected(value):
    row = MULTI.copy()
    row[9] = value
    questions, errors = parse_workbook(workbook([row]))
    assert not questions
    assert errors and errors[0]['row'] == 2


def test_five_answers_fit_existing_column():
    assert ''.join(answer_letters('A,B,C,D,E')) == 'ABCDE'


@pytest.mark.parametrize('answer,score', [([0, 2], 100), ([2, 0], 100), (['A', 'C'], 100),
    ([0], 0), ([0, 1, 2], 0), ([0, 0, 2], 0), ([], 0), ([True, 2], 0), ([0, 9], 0),
    ([0.2, 2], 0), ('A,C', 0), (None, 0)])
def test_exact_set_grading_and_untrusted_answers(answer, score):
    quiz = {'type': 'mcq', 'question': MULTI[3], 'options': MULTI[4:8], 'correct_indices': [0, 2], 'explanation': MULTI[10]}
    result = grade_objective(quiz, answer)
    assert result['score'] == score
    assert result['correct_answer'] == ['HTTP', 'HTTPS']
    assert 'correct_indices' not in result


def test_missing_and_extra_choices_are_explained():
    quiz = {'type': 'mcq', 'options': MULTI[4:8], 'correct_indices': [0, 2]}
    result = grade_objective(quiz, [0, 1])
    assert result['missing_answers'] == ['HTTPS']
    assert result['extra_answers'] == ['SMTP']
    assert 'Missing correct choices: HTTPS' in result['reasoning']
    review = build_review_payload(quiz, [0, 1], result)
    assert review['options'][0]['is_correct'] and review['options'][2]['is_correct']
    assert review['options'][1]['is_selected'] and not review['options'][1]['is_correct']
    assert review['correct_label'] == 'Correct options'


def test_single_answer_zero_and_old_records_remain_supported():
    quiz = {'type': 'mcq', 'options': MULTI[4:8], 'correct_index': 0}
    assert grade_objective(quiz, 0)['score'] == 100
    assert grade_objective(quiz, 'A')['score'] == 100
    review = build_record_review(SimpleNamespace(question_type='mcq', options_or_pairs=json.dumps(quiz),
        answer='0', is_correct=True, score=100, ai_feedback='[KBot Detailed Feedback v1]\nAI grading comment: Recovered old comment.',
        explanation='HTTP transfers web content.', citations_json='[{"source_file":"notes.pdf","page":4,"excerpt":"HTTP transfers web content."}]'))
    assert review['student_answer'] == 'A. HTTP'
    assert review['feedback'] == 'Recovered old comment.'
    assert review['citations'][0]['page'] == 4


@pytest.fixture
def apps(tmp_path, monkeypatch):
    config = {'TESTING': True, 'SECRET_KEY': 'multi-test', 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "app.db"}',
              'TEACHER_CHROMA_ENABLED': False, 'TEACHER_CHROMA_PATH': str(tmp_path / 'chroma'),
              'LEGACY_TEACHER_COMPAT': False, 'SEED_DEMO_TEACHER': False, 'COZE_ENABLED': False}
    teacher = teacher_app(config)
    with teacher.app_context():
        db.session.add(User(username='multi_teacher', role='teacher', password_hash=''))
        db.session.add(User(username='other_teacher', role='teacher', password_hash=''))
        db.session.commit()
        owner = User.query.filter_by(username='multi_teacher').one().id
    chapters = [{'id': 'ch1', 'label': 'Web protocols', 'source_file': 'notes.pdf'}]
    teacher.extensions['knowledge_client'] = SimpleNamespace(chapters=lambda: chapters)
    tc = teacher.test_client()
    with tc.session_transaction() as session:
        session['_user_id'], session['_fresh'] = str(owner), True
    tc.get('/question-bank/')
    student = student_app(config)
    from student_app.blueprints import exercise
    monkeypatch.setattr(exercise, 'knowledge_client', lambda: SimpleNamespace(chapters=lambda: chapters))
    return teacher, tc, student


def import_rows(tc, rows):
    with tc.session_transaction() as session:
        csrf = session['bank_csrf']
    content = workbook(rows)
    fields = lambda: {'csrf': csrf, 'chapter_id': 'ch1', 'file': (io.BytesIO(content), 'multi.xlsx')}
    response = tc.post('/question-bank/preview', data=fields())
    assert response.status_code == 200, response.text
    token = re.search(r'name="preview_token" value="([^"]+)"', response.text).group(1)
    data = fields()
    data['preview_token'] = token
    assert tc.post('/question-bank/confirm', data=data).status_code == 200
    return response


@pytest.mark.parametrize('answer,score,next_type', [([0, 2], 100, 'matching'), ([0], 0, 'mcq'), ([0, 1, 2], 0, 'mcq')])
def test_excel_import_to_student_grading_and_history(apps, answer, score, next_type):
    teacher, tc, student = apps
    preview = import_rows(tc, [TF, MULTI])
    assert 'A, C' in preview.text and 'Select all correct answers' in preview.text
    with teacher.app_context():
        row = BankQuestion.query.filter_by(question_id='WEB1').one()
        assert row.correct_answer == 'AC'
    sc = student.test_client()
    register(sc, 'multi_student')
    assert sc.post('/api/exercise/setup', json={'chapter_id': 'ch1'}).status_code == 200
    tf = sc.post('/api/exercise/generate', json={'quiz_type': 'true_false', 'topic': ''}).get_json()
    assert sc.post('/api/exercise/submit', json={'quiz_id': tf['quiz_id'], 'answer': 0}).get_json()['is_correct']
    response = sc.post('/api/exercise/generate', json={'quiz_type': 'mcq', 'topic': 'web'})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload['quiz']['multiple_answers'] is True
    assert not {'correct_index', 'correct_indices', 'explanation'} & payload['quiz'].keys()
    reused = sc.post('/api/exercise/generate', json={'quiz_type': 'mcq'}).get_json()
    assert reused['quiz_id'] == payload['quiz_id']
    result = sc.post('/api/exercise/submit', json={'quiz_id': payload['quiz_id'], 'answer': answer}).get_json()
    assert result['score'] == score and result['next_type'] == next_type
    assert result['review']['correct_label'] == 'Correct options'
    history = sc.get('/history').text
    assert 'Correct options' in history and 'HTTP' in history and 'HTTPS' in history
    assert 'Feedback:' in history and MULTI[10] in history
    with student.app_context():
        record = StudentQARecord.query.filter_by(question_type='mcq').one()
        stored = json.loads(record.options_or_pairs)
        assert stored['correct_indices'] == [0, 2]
        assert stored['review_feedback']['reasoning']
        detailed = record.ai_feedback
        assert 'A. HTTP [CORRECT' in detailed and 'C. HTTPS [CORRECT' in detailed


def test_downloaded_template_has_executable_multi_answer_example(apps):
    _, tc, _ = apps
    response = tc.get('/question-bank/template')
    book = load_workbook(io.BytesIO(response.data))
    assert 'A,C' in book['Questions']['J1'].comment.text
    example = list(book['Multi-answer example'].values)[1]
    assert example[9] == 'A,C'
    questions, errors = parse_workbook(workbook([example]))
    assert not errors and questions[0]['correct_answer'] == 'AC'


def test_teacher_can_correct_explanation_without_changing_answers(apps):
    teacher, tc, _ = apps
    bad = MULTI.copy()
    bad[10] = 'Only cost reduction is related to Economics.'
    preview = import_rows(tc, [bad])
    assert 'Explanation may not address this question' in preview.text
    with teacher.app_context():
        row_id = BankQuestion.query.filter_by(question_id='WEB1').one().id
    with tc.session_transaction() as session:
        csrf = session['bank_csrf']
    assert tc.post(f'/question-bank/{row_id}/explanation', data={'csrf': 'wrong', 'explanation': 'Changed'}).status_code == 403
    with tc.session_transaction() as session:
        session['_user_id'] = '2'
    assert tc.post(f'/question-bank/{row_id}/explanation', data={'csrf': csrf, 'explanation': 'Changed'}).status_code == 404
    with tc.session_transaction() as session:
        session['_user_id'] = '1'
    assert tc.post(f'/question-bank/{row_id}/explanation', data={'csrf': csrf, 'explanation': MULTI[10]}).status_code == 303
    with teacher.app_context():
        row = db.session.get(BankQuestion, row_id)
        assert row.explanation == MULTI[10] and row.correct_answer == 'AC'


def test_matching_upload_type_matching_is_served(apps):
    teacher, tc, _ = apps
    response = tc.get('/question-bank/template?type=MATCHING')
    rows, errors = parse_workbook(response.data)
    assert not errors and rows[0]['question_type'] == 'MATCHING'
    with teacher.app_context():
        values = {key: value for key, value in rows[0].items() if key not in ('row', 'details')}
        row = BankQuestion(owner_id=1, course='ch1', **values)
        row.details = BankQuestionDetails(data=rows[0]['details'])
        db.session.add(row)
        db.session.commit()
        hit = pick_question('ch1', 'matching')
        assert hit is not None
        assert grade_objective(hit['quiz'], hit['quiz']['correct_mapping'])['score'] == 100


def test_agent_multiple_correct_indices_are_normalised_and_grounded():
    note = 'HTTP transfers web content. HTTPS transfers encrypted web content.'
    chunk = {'text': note}
    quiz = normalize_quiz({'type': 'mcq', 'question': 'Which statements describe web content transfer? Select all correct answers.',
        'options': ['HTTP transfers web content.', 'HTTPS transfers encrypted web content.', 'SMTP sends web content.', 'DNS transfers encrypted web content.'],
        'correct_indices': [0, 1], 'explanation': note}, 'mcq')
    validate_grounding(quiz, [chunk], [], FocusFact(note, 'f1', chunk))
    assert grade_objective(quiz, [1, 0])['score'] == 100
    quiz['correct_indices'] = [0, 9]
    with pytest.raises(QuizValidationError):
        validate_grounding(quiz, [chunk], [])


def test_document_recall_mcq_and_matching_fragments_are_rejected():
    with pytest.raises(QuizValidationError, match='document organization'):
        validate_grounding({'type': 'mcq', 'question': 'Which statement is presented in the course-note section GPS Background?'}, [], [])
    for value in ('Since then…', 'Wikipedia definition', '1997', '2000', 'Overview'):
        assert not _matching_term(value)
    assert _matching_term('Control segment') == 'Control segment'


def test_local_mcq_distractors_share_the_assessed_subject():
    fact = 'A GPS receiver requires 4 satellites for positioning.'
    chunk = {'text': fact, 'facts': [{'text': fact, 'fact_key': 'f1'}]}
    quiz, _ = local_quiz('mcq', FocusFact(fact, 'f1', chunk), [chunk], [], 'GPS')
    assert 'course-note section' not in quiz['question']
    assert all('GPS receiver' in option for option in quiz['options'])
    assert len(quiz['options']) == 4
    validate_grounding(quiz, [chunk], [], FocusFact(fact, 'f1', chunk))


def test_history_escapes_explanations_and_keeps_sources(apps):
    _, _, student = apps
    sc = student.test_client()
    register(sc, 'history_student')
    with student.app_context():
        db.session.add(StudentQARecord(student_name='history_student', question_type='mcq', question='Question?', answer='[0,2]',
            options_or_pairs=json.dumps({'options': MULTI[4:8], 'correct_indices': [0, 2], 'review_feedback': {'reasoning': '<script>alert(1)</script>'}}),
            correct_answer='["HTTP","HTTPS"]', is_correct=True, score=100, explanation='<b>Untrusted</b>',
            generation_source='teacher_upload', citations_json='[{"source_file":"notes.pdf","page":3,"excerpt":"<img src=x>"}]'))
        db.session.commit()
    body = sc.get('/history').text
    assert '&lt;script&gt;' in body and '<script>alert(1)</script>' not in body
    assert 'notes.pdf' in body and 'page 3' in body
    assert 'From question bank' in body


def test_multi_answer_export_never_silently_drops_a_correct_option():
    record = SimpleNamespace(question_type='mcq', question=MULTI[3], options_or_pairs=json.dumps({'options': MULTI[4:8], 'correct_indices': [0, 2]}), correct_answer='["HTTP", "HTTPS"]')
    assert eligible_questions([record]) == []


def test_feedback_waits_for_user_and_checkbox_contract_is_present():
    source = (Path(__file__).resolve().parents[1] / 'student_app/static/app.js').read_text()
    assert 'window.location.assign(result.redirect), 8000' not in source
    assert 'Select all correct answers' in source and 'values.getAll("answer")' in source
