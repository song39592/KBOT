"""Teacher chapter dropdown, Excel preview and file-bound confirmation."""
import io
import re

import pytest
from openpyxl import Workbook

from common.knowledge_client import KnowledgeServiceError
from common.models import BankQuestion, BankQuestionDetails, User, db
from common.question_bank import HEADERS
from teacher_app.app import create_app as teacher_app
from tests.test_http_integration import upload_real_pdf, student_config


def workbook():
    book = Workbook()
    book.active.append(HEADERS)
    book.active.append(['TF1', 'TF', 'GPS', 'GPS provides positioning.', '', '', '', '', '', 'TRUE', 'Positioning.'])
    book.active.append(['MC1', 'MCQ', 'DNS', 'Which resolves names?', 'HTTP', 'DNS', '', '', '', 'B', 'DNS resolves names.'])
    buf = io.BytesIO()
    book.save(buf)
    book.close()
    return buf.getvalue()


@pytest.fixture
def workflow(knowledge_server, real_pdf, tmp_path):
    upload_real_pdf(knowledge_server, real_pdf)
    app = teacher_app({
        **student_config(knowledge_server, tmp_path),
        'KNOWLEDGE_ADMIN_KEY': 'admin-test',
        'TEACHER_CHROMA_ENABLED': False, 'LEGACY_TEACHER_COMPAT': False,
        'SEED_DEMO_TEACHER': False,
    })
    with app.app_context():
        user = User(username='owner', role='teacher', password_hash='')
        db.session.add(user)
        db.session.commit()
        uid = user.id
    teacher = app.test_client()
    with teacher.session_transaction() as state:
        state['_user_id'] = str(uid)
        state['_fresh'] = True
    page = teacher.get('/question-bank/')
    chapter = re.search(r'<option value="([^"]+)"[^>]*>Chapter 3', page.text).group(1)
    with teacher.session_transaction() as state:
        csrf = state['bank_csrf']
    def upload(endpoint, chapter_id=chapter, token='', content=None):
        return teacher.post('/question-bank/' + endpoint, data={
            'csrf': csrf, 'chapter_id': chapter_id, 'preview_token': token,
            'file': (io.BytesIO(workbook() if content is None else content), 'bank.xlsx'),
        })
    return app, teacher, page, chapter, upload


def test_dropdown_import_stores_selected_chapter_id(workflow):
    app, teacher, page, chapter, upload = workflow
    assert '<select id="chapter_id" name="chapter_id" required' in page.text
    assert 'name="course" required maxlength' not in page.text
    preview = upload('preview')
    assert preview.status_code == 200
    assert 'Preview · Chapter 3: Enabling Technologies' in preview.text
    token = re.search('name="preview_token" value="([^"]+)"', preview.text).group(1)
    confirmed = upload('confirm', token=token)
    assert confirmed.status_code == 200
    with app.app_context():
        assert {q.course for q in BankQuestion.query.all()} == {chapter}
    listed = teacher.get(confirmed.json['redirect'])
    assert 'Your imported questions' in listed.text
    assert 'Chapter 3: Enabling Technologies' in listed.text


@pytest.mark.parametrize('bad_chapter', ['', 'Chapter 3: Enabling Technologies', 'invented-id'])
def test_import_rejects_unselected_or_forged_chapter(workflow, bad_chapter):
    app, _, _, _, upload = workflow
    assert upload('preview', bad_chapter).status_code == 400
    with app.app_context():
        assert BankQuestion.query.count() == 0


def test_deleted_chapter_rejected_at_confirmation(workflow):
    app, _, _, _, upload = workflow
    preview = upload('preview')
    token = re.search('name="preview_token" value="([^"]+)"', preview.text).group(1)
    app.extensions['knowledge_client'].chapters = lambda: []
    assert upload('confirm', token=token).status_code == 400
    with app.app_context():
        assert BankQuestion.query.count() == 0


def test_chapter_outage_disables_import_and_retains_listing(workflow):
    app, teacher, _, _, upload = workflow
    def unavailable():
        raise KnowledgeServiceError('offline')
    app.extensions['knowledge_client'].chapters = unavailable
    page = teacher.get('/question-bank/')
    assert page.status_code == 200
    assert 'temporarily unavailable' in page.text
    assert '<select id="chapter_id" name="chapter_id" required disabled>' in page.text
    assert upload('preview').status_code == 503


def test_no_chapters_explains_upload_notes_first(workflow):
    app, teacher, _, _, _ = workflow
    app.extensions['knowledge_client'].chapters = lambda: []
    assert 'Upload course notes' in teacher.get('/question-bank/').text


@pytest.mark.parametrize('kind', ['MATCHING', 'FILL', 'SHORT'])
def test_extended_template_import_uses_selected_real_chapter(workflow, kind):
    app, teacher, _, chapter, upload = workflow
    content = teacher.get('/question-bank/template?type=' + kind).data
    preview = upload('preview', content=content)
    assert preview.status_code == 200
    assert 'Preview · Chapter 3: Enabling Technologies' in preview.text
    token = re.search('name="preview_token" value="([^"]+)"', preview.text).group(1)
    confirmed = upload('confirm', token=token, content=content)
    assert confirmed.status_code == 200
    with app.app_context():
        row = BankQuestion.query.one()
        assert row.course == chapter and row.question_type == kind
        assert row.details.data['schema_version'] == 1
        assert BankQuestionDetails.query.count() == 1
    assert 'Chapter 3: Enabling Technologies' in teacher.get(confirmed.json['redirect']).text


def test_teacher_options_match_existing_student_setup_control(workflow):
    from pathlib import Path
    from html import unescape
    from flask import render_template_string
    app, _, page, _, _ = workflow
    source = (Path(__file__).resolve().parents[1] / 'student_app/templates/exercise_setup.html').read_text()
    control = source[source.index('<div class="select-group">'):source.index('      <button class="btn-primary')]
    with app.app_context():
        student_html = render_template_string(control, chapters=app.extensions['knowledge_client'].chapters())
    def options(html):
        select = re.search(r'<select id="chapter_id".*?</select>', html, re.S).group(0)
        return [(value, re.sub(r'\s+', ' ', unescape(label)).strip())
                for value, label in re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', select, re.S)]
    assert options(page.text) == options(student_html)
    assert ' · pages ' in options(page.text)[1][1]


def test_conflicting_new_and_legacy_chapter_fields_are_rejected(workflow):
    app, teacher, _, chapter, _ = workflow
    with teacher.session_transaction() as state:
        csrf = state['bank_csrf']
    response = teacher.post('/question-bank/preview', data={
        'csrf': csrf, 'chapter_id': chapter, 'course': 'different-chapter',
        'file': (io.BytesIO(workbook()), 'bank.xlsx'),
    })
    assert response.status_code == 400 and 'Conflicting chapter' in response.json['error']
    with app.app_context():
        assert BankQuestion.query.count() == 0
