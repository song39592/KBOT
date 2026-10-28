import io
import re
import zipfile
import pytest
from openpyxl import Workbook, load_workbook
from common.models import BankQuestion, db, User
from common.question_bank import HEADERS, parse_workbook
from teacher_app.app import create_app

TF = ['TF1','TF','HTTP','HTTP is stateless.','','','','','','TRUE','Protocol state.']
MCQ = ['MC1','MCQ','DNS','Which resolves names?','HTTP','DNS','SMTP','FTP','','B','DNS resolves names.']


def workbook(rows, headers=HEADERS):
    book = Workbook(); sheet = book.active; sheet.append(headers)
    for row in rows: sheet.append(row)
    output = io.BytesIO(); book.save(output); book.close()
    return output.getvalue()


@pytest.fixture
def client(tmp_path):
    app = create_app(dict(TESTING=True, SECRET_KEY='bank-test', SQLALCHEMY_DATABASE_URI='sqlite://', TEACHER_CHROMA_ENABLED=False, TEACHER_CHROMA_PATH=str(tmp_path/'chroma'), LEGACY_TEACHER_COMPAT=False, SEED_DEMO_TEACHER=False))
    with app.app_context():
        db.session.add_all([User(username='teacher',role='teacher',password_hash=''),User(username='other',role='teacher',password_hash=''),User(username='student',role='student',password_hash='')]);db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id']='1';s['_fresh']=True
    c.get('/question-bank/')
    yield c, app


def upload(c, endpoint, rows=None, *, content=None, course='COMP101', token='', csrf=None):
    with c.session_transaction() as s: actual_csrf=s['bank_csrf']
    return c.post('/question-bank/'+endpoint, data=dict(csrf=actual_csrf if csrf is None else csrf, course=course, preview_token=token, file=(io.BytesIO(content if content is not None else workbook(rows)), 'bank.xlsx')))


def preview_token(c, rows, **kwargs):
    response=upload(c,'preview',rows,**kwargs)
    assert response.status_code==200
    match=re.search('name="preview_token" value="([^"]+)"',response.text)
    return match.group(1) if match else ''


def test_template_roundtrip(client):
    c,_=client;response=c.get('/question-bank/template')
    questions,errors=parse_workbook(response.data)
    assert len(questions)==2 and not errors
    assert questions[0]['correct_answer']=='TRUE'
    assert questions[1]['correct_answer']=='B'


@pytest.mark.parametrize('field,value,message',[(9,'A,B','existing option'),(5,'HTTP','Duplicate option'),(1,'Matching','TF or MCQ'),(3,'','required')])
def test_row_errors(field,value,message):
    bad=MCQ.copy();bad[field]=value
    questions,errors=parse_workbook(workbook([TF,bad]))
    assert len(questions)==1 and errors[0]['row']==3 and message in ' '.join(errors[0]['errors'])


def test_tf_and_duplicate_ids():
    bad=TF.copy();bad[0]='tf1';bad[4]='option';bad[9]='maybe'
    _,errors=parse_workbook(workbook([TF,bad]))
    assert len(errors[0]['errors'])==3


def test_formula_and_optional_e():
    bad=TF.copy();bad[3]='=1+1'
    assert 'Formulas' in parse_workbook(workbook([bad]))[1][0]['errors'][0]
    q=MCQ.copy();q[8]='None of these';q[9]='E'
    assert parse_workbook(workbook([q]))[0][0]['correct_answer']=='E'


def test_invalid_workbooks():
    for data in [b'not excel',workbook([],headers=['bad']),workbook([])]:
        with pytest.raises(ValueError):parse_workbook(data)
    with pytest.raises(ValueError):parse_workbook(b'x'*(5*1024*1024+1))
    with pytest.raises(ValueError):parse_workbook(workbook([TF]*1001))
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:archive.writestr('oversize',b'0'*(20*1024*1024+1))
    with pytest.raises(ValueError,match='Expanded'):parse_workbook(output.getvalue())


def test_preview_confirm_and_duplicate(client):
    c,app=client;token=preview_token(c,[TF,MCQ])
    with app.app_context():assert BankQuestion.query.count()==0
    response=upload(c,'confirm',[TF,MCQ],token=token)
    assert response.status_code==200 and response.json['count']==2
    with app.app_context():
        q=BankQuestion.query.filter_by(question_id='MC1').one()
        assert q.correct_answer=='B' and q.options['B']=='DNS' and q.source=='teacher_upload'
    assert upload(c,'confirm',[TF,MCQ],token=token).status_code==400
    assert not preview_token(c,[TF,MCQ])


def test_confirmation_binds_file_and_course(client):
    c,app=client;token=preview_token(c,[TF])
    assert upload(c,'confirm',[MCQ],token=token).status_code==400
    assert upload(c,'confirm',[TF],token=token,course='OTHER').status_code==400
    assert upload(c,'confirm',[TF]).status_code==400
    assert upload(c,'confirm',[TF],token=token+'bad').status_code==400
    with app.app_context():assert BankQuestion.query.count()==0


def test_invalid_batch_does_not_import(client):
    c,app=client;bad=MCQ.copy();bad[9]='E'
    assert not preview_token(c,[TF,bad])
    assert upload(c,'confirm',[TF,bad]).status_code==400
    with app.app_context():assert BankQuestion.query.count()==0


def test_permissions_csrf_and_owner_isolation(client):
    c,app=client
    assert upload(c,'preview',[TF],csrf='wrong').status_code==403
    token=preview_token(c,[TF]);assert upload(c,'confirm',[TF],token=token).status_code==200
    with c.session_transaction() as s:s['_user_id']='2'
    assert 'HTTP is stateless' not in c.get('/question-bank/').text
    assert preview_token(c,[TF])  # another teacher may use the same course/ID
    with c.session_transaction() as s:s['_user_id']='3'
    assert c.get('/question-bank/').status_code==403
    assert c.get('/question-bank/template').status_code==403
    with c.session_transaction() as s:s.pop('_user_id')
    assert c.get('/question-bank/').status_code==302


def test_proxy_prefix_and_escaped_preview(client):
    c,_=client;response=c.get('/question-bank/',headers={'X-Forwarded-Prefix':'/teacher'})
    assert 'action="/teacher/question-bank/preview"' in response.text
    assert 'href="/teacher/question-bank/template"' in response.text
    assert 'href="/teacher/"' in response.text
    bad=TF.copy();bad[3]='<script>alert(1)</script>'
    response=upload(c,'preview',[bad])
    assert '&lt;script&gt;' in response.text and '<script>' not in response.text


def test_blank_file_request(client):
    c,_=client
    with c.session_transaction() as s:csrf=s['bank_csrf']
    assert c.post('/question-bank/preview',data={'csrf':csrf,'course':'COMP101'}).status_code==400


def test_pagination_and_course_filter(client):
    c,app=client
    with app.app_context():
        for i in range(26):db.session.add(BankQuestion(owner_id=1,course='COMP101',question_id=str(i),question_type='TF',question='Bank item '+str(i),correct_answer='TRUE',options={}))
        db.session.commit()
    assert 'Page 1 of 2' in c.get('/question-bank/').text
    assert 'Page 2 of 2' in c.get('/question-bank/?page=2').text
    assert 'No questions yet' in c.get('/question-bank/?course=OTHER').text


def test_unicode_csrf_is_rejected(client):
    c,_=client
    assert upload(c,'preview',[TF],csrf='错误').status_code==403


def test_new_preview_invalidates_older_confirmation(client):
    c,_=client;old=preview_token(c,[TF]);new=preview_token(c,[MCQ])
    assert upload(c,'confirm',[TF],token=old).status_code==400
    assert upload(c,'confirm',[MCQ],token=new).status_code==200
