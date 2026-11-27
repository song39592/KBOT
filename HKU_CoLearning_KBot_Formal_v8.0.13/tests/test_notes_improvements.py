import json
import pytest
from common.notes_engine import normalize_notes
from common.models import db, User, NotesQARecord, NotesDetail, NotesFeedback
from student_app.app import create_app as student_app
from teacher_app.app import create_app as teacher_app


def test_sections():
    result = normalize_notes({"sections":[{"kind":"course","text":"DNS resolves names.","reference_ids":[1,999]}, {"kind":"outside_course","text":"Extra background.","reference_ids":[1]}]}, [{"reference_id":1}])
    assert result["sections"][0]["reference_ids"] == [1]
    assert result["sections"][1]["reference_ids"] == []
    with pytest.raises(ValueError):
        normalize_notes({"sections":[{"kind":"course","text":"Unsupported","reference_ids":[999]}]}, [{"reference_id":1}])


def test_feedback_and_ranking(tmp_path):
    config = {"TESTING":True,"SECRET_KEY":"test", "SQLALCHEMY_DATABASE_URI":"sqlite:///"+str(tmp_path/'app.db'),"COZE_ENABLED":False,"TEACHER_CHROMA_ENABLED":False,"LEGACY_TEACHER_COMPAT":False}
    app = student_app(config)
    teacher = teacher_app(config)
    with app.app_context():
        user = User(username="teacher",role="teacher"); user.set_password("password")
        db.session.add(user)
        for id, name, topic in [("a","alice","dns"),("b","bob","dns"),("c","alice","tcp")]:
            db.session.add(NotesQARecord(id=id,student_name=name,question="What is DNS?", answer="Answer",chapter_id="chapter"))
            db.session.flush()
            db.session.add(NotesDetail(record_id=id,topic=topic))
        db.session.commit()
        uid = user.id
    client=app.test_client()
    assert client.post('/api/notes/a/feedback',json={}).status_code == 401
    with client.session_transaction() as session: session['student_username']='alice'
    payload={"category":"incorrect","comment":"Wrong explanation"}
    assert client.post('/api/notes/b/feedback',json=payload).status_code == 404
    assert client.post('/api/notes/a/feedback',json=payload).status_code == 200
    assert client.post('/api/notes/a/feedback',json=payload).status_code == 409
    tc=teacher.test_client()
    assert tc.get('/notes-review').status_code in (302,401)
    with tc.session_transaction() as session: session['_user_id']=str(uid);session['_fresh']=True
    response=tc.get('/notes-review')
    assert response.status_code == 200
    assert b'Wrong explanation' in response.data and b'dns' in response.data
    assert response.data.index(b'<td>dns</td>') < response.data.index(b'<td>tcp</td>')
    with app.app_context(): fid=NotesFeedback.query.first().id
    assert tc.post('/notes-review/'+fid+'/resolve').status_code == 400
    with tc.session_transaction() as session: token=session['notes_review_csrf']
    assert tc.post('/notes-review/'+fid+'/resolve',data={'csrf':token,'correction':'Corrected explanation from teacher.'}).status_code == 302
    assert b'Wrong explanation' not in tc.get('/notes-review').data
    assert b'Wrong explanation' in tc.get('/notes-review?status=resolved').data
    history = client.get('/notes/history')
    assert b'Corrected explanation from teacher.' in history.data
    with client.session_transaction() as session: token = session['notes_csrf']
    assert client.post('/notes/a/bookmark',data={'csrf':token,'saved':'1'}).status_code==302
    assert b'What is DNS?' in client.get('/notes/history?saved=1').data
    assert client.post('/notes/b/bookmark',data={'csrf':token,'saved':'1'}).status_code==404
    assert client.get('/notes/b/reference/1').status_code==404
    with app.app_context(): assert db.session.get(NotesQARecord,'a').answer=='Answer'



def test_question_saved_with_normalized_topic(tmp_path):
    from types import SimpleNamespace
    app = student_app({"TESTING":True,"SECRET_KEY":"test","COZE_ENABLED":False,"SQLALCHEMY_DATABASE_URI":"sqlite:///"+str(tmp_path/'ask.db')})
    app.extensions["knowledge_client"] = SimpleNamespace(search=lambda *args: {"results":[{"text":"DNS translates domain names into IP addresses for network communication.","source_file":"network.txt","chapter":"Networks","page":1}]})
    client=app.test_client()
    with client.session_transaction() as session: session['student_username']='alice'
    data=client.post('/api/notes/ask',json={"question":"What is DNS?","topic":"  DNS  ","chapter_id":"c"}).get_json()
    assert data['ok'] and data['record_id'] and data['sections'][0]['reference_ids']==[1]
    with app.app_context():
        assert db.session.get(NotesDetail,data['record_id']).topic=='dns'
        assert db.session.get(NotesQARecord,data['record_id']).student_name=='alice'


def test_structured_agent_and_invalid_citation_fallback():
    from types import SimpleNamespace
    from common.notes_engine import answer_notes
    chunks=[{"text":"DNS translates domain names into IP addresses for network communication."}]
    router=SimpleNamespace(config=lambda kind: SimpleNamespace(configured=True), request=lambda *args: SimpleNamespace(content=json.dumps({"sections":[{"kind":"course","text":"DNS resolves names.","reference_ids":[1]},{"kind":"outside_course","text":"Additional context.","reference_ids":[]}]})))
    result=answer_notes('What is DNS?',chunks,router,'alice')
    assert result['answering_source']=='coze_notes_agent'
    assert result['sections'][1]['kind']=='outside_course'
    router.request=lambda *args: SimpleNamespace(content=json.dumps({"sections":[{"kind":"course","text":"Bad citation","reference_ids":[99]}]}))
    assert answer_notes('What is DNS?',chunks,router,'alice')['answering_source']=='local_fallback'


@pytest.mark.parametrize("status,sections,missing,clarification,expected", [
    ("supported",[{"kind":"course","text":"Fact","reference_ids":[1]}],"","","supported"),
    ("partial",[{"kind":"course","text":"Fact","reference_ids":[1]}],"UDP overhead is missing","","partial"),
    ("clarify",[],"","Which protocol?","clarify"),
    ("supported",[{"kind":"outside_course","text":"Extra","reference_ids":[]}],"","","unsupported"),
])
def test_evidence_policy(status,sections,missing,clarification,expected):
    from common.notes_engine import apply_evidence_policy
    result=apply_evidence_policy(dict(evidence_status=status,sections=sections,missing_information=missing,clarification_question=clarification))
    assert result['evidence_status']==expected
    if expected=='partial': assert result['missing_information']==missing
    if expected in {'clarify','unsupported'}: assert result['used_reference_ids']==[]


def test_vague_question_requests_clarification():
    from common.notes_engine import answer_notes
    result=answer_notes('Explain this',[],None,'student')
    assert result['evidence_status']=='clarify'
    assert result['sections']==[]


def test_reference_highlight_and_authorization(tmp_path):
    from types import SimpleNamespace
    app=student_app({"TESTING":True,"SECRET_KEY":"test","COZE_ENABLED":False,"SQLALCHEMY_DATABASE_URI":"sqlite:///"+str(tmp_path/'refs.db')})
    app.extensions['knowledge_client']=SimpleNamespace(_request=lambda *args: {'pages':[{'page':2,'text':'Before DNS resolves names. After <script>bad</script>'}]})
    with app.app_context():
        db.session.add(NotesQARecord(id='ref',student_name='alice',question='DNS?',answer='Original',citations_json=json.dumps([{'reference_id':1,'document_id':'doc','source_file':'notes.pdf','page':2,'excerpt':'DNS resolves names.'}])))
        db.session.commit()
    c=app.test_client()
    with c.session_transaction() as session: session['student_username']='alice'
    response=c.get('/notes/ref/reference/1')
    assert response.status_code==200
    assert b'<mark id="citation-hit">DNS resolves names.</mark>' in response.data
    assert b'#page=2' in response.data and b'<script>bad</script>' not in response.data
    with c.session_transaction() as session: session['student_username']='bob'
    assert c.get('/notes/ref/reference/1').status_code==404


def test_deepseek_adapter(monkeypatch):
    from common.deepseek_notes import DeepSeekNotesRouter
    from common.coze_transport import CozeTransportError
    from types import SimpleNamespace
    def post(url, **kwargs):
        if url.endswith('/tools/search'):
            return SimpleNamespace(status_code=200,json=lambda:{'results':[{'url':'https://example.org/dns','title':'DNS','summary':'DNS resolves names'}]})
        assert url=='https://api.deepseek.com/chat/completions'
        assert kwargs['json']['response_format']=={'type':'json_object'}
        return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop','message':{'content':'{"sections":[]}'}}]})
    monkeypatch.setattr('common.deepseek_notes.requests.post',post)
    router=DeepSeekNotesRouter('test-key', web_search=True)
    response = router.request(None,'Return JSON','user')
    assert response.content=='{"sections":[]}'
    assert response.search_status=='completed'
    assert response.web_sources[0]['url']=='https://example.org/dns'
    assert not DeepSeekNotesRouter('').config(None).configured
    monkeypatch.setattr('common.deepseek_notes.requests.post',lambda *a,**k:SimpleNamespace(status_code=401))
    with pytest.raises(CozeTransportError,match='401'):
        router.request(None,'JSON','user')


def test_deepseek_default_never_searches(monkeypatch):
    from common.deepseek_notes import DeepSeekNotesRouter
    from types import SimpleNamespace
    calls=[]
    def post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200,json=lambda:{'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    monkeypatch.setattr('common.deepseek_notes.requests.post',post)
    response=DeepSeekNotesRouter('key').request(None,'Question','student')
    assert calls==['https://api.deepseek.com/chat/completions']
    assert response.search_status=='not_requested'


@pytest.mark.parametrize('question',['what is DNF?', 'What is dnf?', 'DNF', 'Explain XYZ'])
def test_unknown_bare_acronym_clarifies_without_call(question):
    from common.notes_engine import answer_notes
    result=answer_notes(question,[{'text':'DNS translates domain names into IP addresses.'}],None,'alice')
    assert result['evidence_status']=='clarify'
    assert result['sections']==[]


def test_acronym_context_and_transposition():
    from common.notes_engine import bare_acronym, answer_notes
    from types import SimpleNamespace
    assert bare_acronym('What is DNF in propositional logic?') is None
    router=SimpleNamespace(web_search=True,config=lambda _:SimpleNamespace(configured=True), request=lambda *a:SimpleNamespace(content=json.dumps({'sections':[{'kind':'outside_course','text':'DFN is a network.','reference_ids':[]}]})))
    result=answer_notes('What is DNF?',[],router,'alice')
    assert result['evidence_status']=='clarify'
    assert 'DNF' in result['answer'] and 'DFN is' not in result['answer']
