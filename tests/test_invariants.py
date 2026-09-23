import time
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.app import jobs
from backend.app.config import settings
from backend.app.contracts import Extraction
from backend.app.db import Event, Job, Meeting, Session, User
from backend.app.deadlines import normalize
from backend.app.main import app
from backend.app.security import require_meeting, scope_token, scoped_job


def seed():
    with Session.begin() as db:
        meeting = Meeting(id='meeting', owner_id='owner', title='Test', revision=1,
                          document={'segments': [{'id': 'seg1', 'text': 'Айдана готовит отчёт к пятнице.', 'speaker': 'speaker1', 'start_ms': 0, 'end_ms': 1000}], 'participants': ['Айдана'], 'timezone': 'Asia/Qyzylorda', 'started_at': '2026-09-23T10:00:00+05:00'})
        db.add(meeting)
        db.add(Event(id='event1', kind='transcript.ready', meeting_id='meeting', revision=1, user_id='owner', payload={}))


def test_event_replay_creates_one_run():
    seed()
    jobs.dispatch_event('event1')
    jobs.dispatch_event('event1')
    with Session() as db:
        assert db.query(Job).count() == 1


def test_lease_scope_acl_and_stale_revision():
    seed();jobs.dispatch_event('event1')
    claimed = jobs.claim(jobs.AI_KINDS)
    assert claimed['has_work']
    assert not jobs.claim(jobs.AI_KINDS)['has_work']
    with Session.begin() as db:
        job = scoped_job(db, claimed['scope_token'])
        assert job.user_id == 'owner'
        with pytest.raises(HTTPException): require_meeting(db, 'meeting', 'stranger')
        with pytest.raises(HTTPException): scoped_job(db, claimed['scope_token']+'x')
        db.get(Meeting, 'meeting').revision = 2
    with Session() as db:
        with pytest.raises(HTTPException) as exc: scoped_job(db, claimed['scope_token'])
        assert exc.value.status_code == 409


def test_source_validation_and_idempotent_commit():
    seed();jobs.dispatch_event('event1');claimed=jobs.claim(jobs.AI_KINDS)
    invalid={'actions':[{'title':'Отчёт','assignee_mention':'Ерлан','due_raw':None,'source_segment_ids':['seg1']}]}
    with Session() as db:
        job=scoped_job(db,claimed['scope_token'])
        with pytest.raises(ValueError): jobs.validate(db,job,invalid)
        invalid['actions'][0]['source_segment_ids']=['foreign-id']
        with pytest.raises(ValueError): jobs.validate(db,job,invalid)
    good={'actions':[{'title':'Подготовить отчёт','assignee_mention':'Айдана','due_raw':'к пятнице','source_segment_ids':['seg1']}]}
    assert jobs.commit(claimed['scope_token'],good)['status']=='succeeded'
    assert jobs.commit(claimed['scope_token'],good)['status']=='succeeded'
    with Session() as db:
        assert db.query(Job).filter_by(kind='reconcile').count()==1


def test_no_dropped_candidates():
    seed();jobs.dispatch_event('event1');claimed=jobs.claim(jobs.AI_KINDS)
    jobs.commit(claimed['scope_token'],{'actions':[{'title':'Отчёт','assignee_mention':'Айдана','due_raw':None,'source_segment_ids':['seg1']}]})
    claim2=jobs.claim(jobs.AI_KINDS)
    with Session() as db:
        with pytest.raises(ValueError):jobs.validate(db,scoped_job(db,claim2['scope_token']),{'groups':[]})


def test_unknown_dates_and_future_payment_terms():
    assert normalize('к пятнице',None)['date'] is None
    assert normalize('15 октября',None)['date'] is None
    assert normalize('к пятнице','2026-09-23T10:00:00+05:00')['date']=='2026-09-25'
    assert normalize('оплата в течение пяти рабочих дней','2026-09-23')['date'] is None
    assert normalize(None,'2026-09-23')['kind']=='missing'


def test_api_auth_and_recording_consent():
    with TestClient(app) as client:
        assert client.get('/api/meetings').status_code==401
        assert client.post('/internal/work/claim').status_code==401
        assert client.post('/api/login',json={'email':settings.admin_email,'password':settings.admin_password}).status_code==200
        assert client.post('/api/meetings',json={'title':'Test','consent':False}).status_code==422
        assert client.post('/api/meetings',json={'title':'Test','consent':True}).status_code==200


def test_no_silent_chunk_truncation():
    segments=[{'id':str(i),'text':'абв '*70} for i in range(20)]
    chunks=jobs.chunks(segments,1000)
    assert [s['id'] for batch in chunks for s in batch]==[s['id'] for s in segments]
    with pytest.raises(HTTPException):jobs.chunks([{'id':'long','text':'а'*6000}],1000)


def test_expired_attempt_cannot_commit_after_recovery():
    seed(); jobs.dispatch_event('event1'); first = jobs.claim(jobs.AI_KINDS)
    with Session.begin() as db:
        db.get(Job, first['work_unit_id']).expires = time.time()-1
    assert jobs.reconcile()['expired_leases'] == 1
    second = jobs.claim(jobs.AI_KINDS)
    assert second['work_unit_id'] == first['work_unit_id']
    assert second['scope_token'] != first['scope_token']
    with pytest.raises(HTTPException):
        jobs.commit(first['scope_token'], {'actions': []})
    assert jobs.commit(second['scope_token'], {'actions': []})['status'] == 'succeeded'


def test_speaker_mapping_requires_known_names_and_fresh_revision():
    with TestClient(app) as client:
        client.post('/api/login', json={'email': settings.admin_email, 'password': settings.admin_password})
        meeting = client.post('/api/meetings', json={'title': 'Speaker test', 'consent': True, 'participants': ['Айдана']}).json()
        mid = meeting['id']
        transcript = client.post(f'/api/meetings/{mid}/transcript', json={'text': 'Сделаю отчёт.', 'expected_revision': 1}).json()
        speaker = transcript['document']['segments'][0]['speaker']
        revision = transcript['revision']
        assert client.post(f'/api/meetings/{mid}/speaker-map', json={'mapping': {speaker: 'Чужое имя'}, 'expected_revision': revision}).status_code == 422
        assert client.post(f'/api/meetings/{mid}/speaker-map', json={'mapping': {speaker: 'Айдана'}, 'expected_revision': revision-1}).status_code == 409
        updated = client.post(f'/api/meetings/{mid}/speaker-map', json={'mapping': {speaker: 'Айдана'}, 'expected_revision': revision})
        assert updated.status_code == 200
        assert updated.json()['document']['segments'][0]['speaker_confirmed'] is True
        assert updated.json()['revision'] == revision+1


def test_workflow_errors_persist_metadata_without_transcripts():
    with TestClient(app) as client:
        response = client.post('/internal/workflow-errors', headers={'Authorization': f'Bearer {settings.service_token}'},
                               json={'execution_id': '123', 'workflow_id': 'WF03', 'transcript': 'private content'})
        assert response.status_code == 200
    with Session() as db:
        event = db.query(Event).filter_by(kind='workflow.error').one()
        assert event.payload == {'execution_id': '123', 'workflow_id': 'WF03'}
        assert event.state == 'recorded'
