import copy
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, text
from . import jobs, tools
from .config import settings
from .db import Access, Event, Job, LoginSession, Meeting, Notification, Reference, Session, Snapshot, ToolAudit, User, initialize, uid
from .security import current_user, digest, password_hash, password_valid, require_meeting, scoped_job, service_auth


@asynccontextmanager
async def lifespan(app):
    initialize()
    with Session.begin() as db:
        if not db.scalar(select(User).where(User.email == settings.admin_email)):
            db.add(User(email=settings.admin_email, password=password_hash(settings.admin_password), is_admin=True))
    yield


app = FastAPI(title='HackAlem Local Meetings', lifespan=lifespan)
internal = [Depends(service_auth)]


class Login(BaseModel):
    email: str
    password: str


class MeetingInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    started_at: str | None = None
    timezone: str = 'Asia/Qyzylorda'
    participants: list[str] = Field(default_factory=list, max_length=50)
    consent: bool


class TranscriptInput(BaseModel):
    text: str = Field(min_length=1, max_length=500000)
    expected_revision: int


class WorkInput(BaseModel):
    scope_token: str
    output: str | dict = ''


def meeting_json(meeting):
    return {'id': meeting.id, 'title': meeting.title, 'revision': meeting.revision, 'state': meeting.state,
            'confirmed_revision': meeting.confirmed_revision, 'indexed_revision': meeting.indexed_revision,
            'document': meeting.document}


@app.get('/health/live')
def live():
    return {'status': 'ok'}


@app.get('/health/ready')
def ready():
    with Session() as db:
        db.execute(text('SELECT 1'))
    return {'status': 'ready', 'model': settings.llm_model}


@app.post('/api/login')
def login(body: Login, response: Response):
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.email == body.email.lower()))
        if not user or not password_valid(body.password, user.password):
            raise HTTPException(401, 'Неверный логин или пароль')
        token = secrets.token_urlsafe(40)
        db.add(LoginSession(token_hash=digest(token), user_id=user.id, expires=time.time() + 12*3600))
        response.set_cookie('hackalem_session', token, httponly=True, samesite='strict', max_age=12*3600)
        return {'email': user.email}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with Session.begin() as db:
        record = db.get(LoginSession, digest(request.cookies.get('hackalem_session', '')))
        if record:
            db.delete(record)
    response.delete_cookie('hackalem_session')
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(current_user)):
    return {'id': user.id, 'email': user.email, 'is_admin': user.is_admin}


@app.get('/api/system')
def system(user=Depends(current_user)):
    status = {}
    for name, url, headers in [('ollama', f'{settings.ollama_url}/api/tags', {}), ('qdrant', f'{settings.qdrant_url}/collections', {'api-key': settings.qdrant_api_key})]:
        try:
            r = httpx.get(url, headers=headers, timeout=5)
            r.raise_for_status()
            status[name] = {'ready': True}
            if name == 'ollama':
                status[name]['models'] = [m['name'] for m in r.json().get('models', [])]
        except Exception:
            status[name] = {'ready': False}
    return {**status, 'llm_model': settings.llm_model, 'asr_ready': Path(settings.asr_model).exists(), 'diarization_ready': Path(settings.speaker_model).exists()}


@app.post('/api/users')
def create_user(body: Login, user=Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(403, 'Только администратор')
    if len(body.password) < 12 or '@' not in body.email:
        raise HTTPException(422, 'Укажите email и пароль не короче 12 символов')
    with Session.begin() as db:
        if db.scalar(select(User).where(User.email == body.email.lower())):
            raise HTTPException(409, 'Пользователь уже существует')
        other = User(email=body.email.lower(), password=password_hash(body.password))
        db.add(other); db.flush()
        return {'id': other.id, 'email': other.email}


@app.put('/api/meetings/{meeting_id}/access')
def grant_access(meeting_id: str, body: dict, user=Depends(current_user)):
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        if meeting.owner_id != user.id:
            raise HTTPException(403, 'Только владелец меняет доступ')
        other = db.scalar(select(User).where(User.email == str(body.get('email', '')).lower()))
        if not other:
            raise HTTPException(404, 'Пользователь не найден')
        role = body.get('role')
        if role not in ('reader', 'editor', 'none'):
            raise HTTPException(422, 'Роль reader, editor или none')
        access = db.scalar(select(Access).where(Access.meeting_id == meeting_id, Access.user_id == other.id))
        if role == 'none':
            if access: db.delete(access)
        elif access:
            access.role = role
        else:
            db.add(Access(meeting_id=meeting_id, user_id=other.id, role=role))
        return {'updated': True}


@app.get('/api/meetings')
def meetings(user=Depends(current_user)):
    with Session() as db:
        grants = select(Access.meeting_id).where(Access.user_id == user.id)
        found = db.scalars(select(Meeting).where(Meeting.deleted == False, (Meeting.owner_id == user.id) | Meeting.id.in_(grants)).order_by(Meeting.created.desc()))
        return [meeting_json(m) for m in found]


@app.post('/api/meetings')
def create_meeting(body: MeetingInput, user=Depends(current_user)):
    if not body.consent:
        raise HTTPException(422, 'Подтвердите уведомление участников и право обработки')
    try:
        ZoneInfo(body.timezone)
        if body.started_at:
            from datetime import datetime
            datetime.fromisoformat(body.started_at)
    except (ValueError, KeyError):
        raise HTTPException(422, 'Проверьте дату и часовой пояс')
    with Session.begin() as db:
        meeting = Meeting(owner_id=user.id, title=body.title, document={**body.model_dump(exclude={'title'}), 'segments': [], 'actions': [], 'summary': [], 'warnings': []})
        db.add(meeting)
        db.flush()
        return meeting_json(meeting)


@app.get('/api/meetings/{meeting_id}')
def get_meeting(meeting_id: str, user=Depends(current_user)):
    with Session() as db:
        meeting = require_meeting(db, meeting_id, user.id)
        result = meeting_json(meeting)
        result['jobs'] = [{'id': j.id, 'kind': j.kind, 'state': j.state, 'error': j.error, 'attempts': j.attempts, 'revision': j.revision, 'result': j.result if j.kind in ('qa', 'export') else {}, 'tool_calls': j.tool_calls} for j in db.scalars(select(Job).where(Job.meeting_id == meeting_id).order_by(Job.created))]
        return result


def cancel_old(db, meeting):
    for old in db.scalars(select(Job).where(Job.meeting_id == meeting.id, Job.state.in_(['queued', 'running']))):
        old.state = 'canceled'
    meeting.confirmed_revision = None
    meeting.indexed_revision = None


@app.post('/api/meetings/{meeting_id}/transcript')
def transcript(meeting_id: str, body: TranscriptInput, user=Depends(current_user)):
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        if meeting.revision != body.expected_revision:
            raise HTTPException(409, 'Документ изменился; обновите страницу')
        cancel_old(db, meeting)
        meeting.revision += 1
        doc = copy.deepcopy(meeting.document)
        lines = [line.strip() for line in body.text.splitlines() if line.strip()]
        segments = []
        for line in lines:
            speaker, content = line.split(':', 1) if ':' in line[:100] else ('SPEAKER_UNKNOWN', line)
            # Hard split long manual turns, retain speaker and distinct source IDs.
            for start in range(0, len(content), 1400):
                segments.append({'id': uid(), 'speaker': speaker.strip(), 'text': content[start:start+1400].strip(), 'start_ms': 0, 'end_ms': 0, 'timing_source': 'manual'})
        doc.update(segments=segments, actions=[], summary=[], warnings=[], input_mode='manual_transcript')
        meeting.document, meeting.state = doc, 'processing'
        jobs.emit(db, 'transcript.ready', meeting, user.id)
        return meeting_json(meeting)


@app.post('/api/meetings/{meeting_id}/media')
async def media(meeting_id: str, file: UploadFile = File(...), user=Depends(current_user)):
    with Session() as db:
        require_meeting(db, meeting_id, user.id, write=True, lock=True)
    folder = settings.data_dir / 'media' / meeting_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{uid()}.upload'
    size = 0
    try:
        with path.open('wb') as out:
            while block := await file.read(1024*1024):
                size += len(block)
                if size > 500*1024*1024:
                    raise HTTPException(413, 'Максимальный размер записи — 500 МБ')
                out.write(block)
        with Session.begin() as db:
            meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
            cancel_old(db, meeting)
            meeting.revision += 1
            meeting.state = 'processing'
            meeting.document = {**meeting.document, 'media_filename': path.name, 'media_original_name': file.filename, 'segments': [], 'actions': [], 'summary': []}
            jobs.emit(db, 'media.uploaded', meeting, user.id, {'filename': path.name})
        return {'status': 'queued'}
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.post('/api/meetings/{meeting_id}/speaker-map')
def speaker_map(meeting_id: str, body: dict, user=Depends(current_user)):
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        if meeting.revision != body.get('expected_revision'):
            raise HTTPException(409, 'Обновите документ перед изменением говорящих')
        doc = copy.deepcopy(meeting.document)
        mapping = body.get('mapping', {})
        speaker_ids = {s.get('speaker_id', s['speaker']) for s in doc.get('segments', [])}
        if not isinstance(mapping, dict) or not mapping or not set(mapping) <= speaker_ids or any(name not in doc.get('participants', []) for name in mapping.values()):
            raise HTTPException(422, 'Выберите имена из списка участников')
        for segment in doc.get('segments', []):
            original = segment.setdefault('speaker_id', segment['speaker'])
            if original in mapping:
                segment['speaker'], segment['speaker_confirmed'] = mapping[original], True
        cancel_old(db, meeting)
        meeting.revision += 1
        doc.update(actions=[], summary=[], warnings=[])
        meeting.document, meeting.state = doc, 'processing'
        jobs.emit(db, 'transcript.ready', meeting, user.id)
        return meeting_json(meeting)


@app.get('/api/meetings/{meeting_id}/audio')
def audio(meeting_id: str, user=Depends(current_user)):
    with Session() as db:
        meeting = require_meeting(db, meeting_id, user.id)
        filename = meeting.document.get('media_filename')
        if not filename:
            raise HTTPException(404, 'Запись отсутствует')
        path = settings.data_dir / 'media' / meeting_id / 'audio.wav'
        if not path.exists():
            raise HTTPException(404, 'Запись ещё обрабатывается')
        return FileResponse(path, media_type='audio/wav')


@app.put('/api/meetings/{meeting_id}/review')
def edit_review(meeting_id: str, body: dict, user=Depends(current_user)):
    from .contracts import Action, Fact
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        if meeting.revision != body.get('expected_revision') or meeting.state not in ('ready_for_review', 'confirmed'):
            raise HTTPException(409, 'Проверьте состояние и ревизию')
        doc = copy.deepcopy(meeting.document)
        allowed = {s['id'] for s in doc['segments']}
        actions = body.get('actions', doc.get('actions', []))
        for action in actions:
            Action.model_validate({k: action.get(k) for k in ('title', 'assignee_mention', 'due_raw', 'source_segment_ids')})
            if not set(action['source_segment_ids']) <= allowed:
                raise HTTPException(422, 'Недопустимые источники')
            action.setdefault('id', uid())
            action.setdefault('status', 'open')
            if action['status'] not in ('open', 'in_progress', 'done'):
                raise HTTPException(422, 'Недопустимый статус')
            if action.get('due', {}).get('date'):
                date.fromisoformat(action['due']['date'])
        summary = body.get('summary', doc.get('summary', []))
        for item in summary:
            Fact.model_validate(item)
            if not set(item['source_segment_ids']) <= allowed:
                raise HTTPException(422, 'Недопустимые источники')
        doc.update(actions=actions, summary=summary)
        cancel_old(db, meeting)
        meeting.revision += 1
        meeting.document, meeting.state = doc, 'ready_for_review'
        return meeting_json(meeting)


@app.post('/api/meetings/{meeting_id}/confirm')
def confirm(meeting_id: str, body: dict, user=Depends(current_user)):
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        if meeting.revision != body.get('expected_revision'):
            raise HTTPException(409, 'Устаревшая ревизия')
        if meeting.state == 'confirmed':
            return meeting_json(meeting)
        if meeting.state != 'ready_for_review':
            raise HTTPException(409, 'Дождитесь анализа и проверьте черновик')
        doc = copy.deepcopy(meeting.document)
        for action in doc.get('actions', []):
            action['needs_review'] = False
            if action.get('due'):
                action['due']['needs_review'] = False
        meeting.document, meeting.state, meeting.confirmed_revision = doc, 'confirmed', meeting.revision
        db.add(Snapshot(meeting_id=meeting.id, revision=meeting.revision, document=copy.deepcopy(doc)))
        jobs.emit(db, 'meeting.confirmed', meeting, user.id)
        return meeting_json(meeting)


@app.post('/api/meetings/{meeting_id}/questions')
def question(meeting_id: str, body: dict, user=Depends(current_user)):
    value = str(body.get('question', '')).strip()
    if not value or len(value) > 1000:
        raise HTTPException(422, 'Вопрос: 1–1000 символов')
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id)
        if meeting.state != 'confirmed' or meeting.indexed_revision != meeting.revision:
            raise HTTPException(409, 'Подтвердите протокол и дождитесь индексирования')
        event = jobs.emit(db, 'question.created', meeting, user.id, {'question': value})
        return {'event_id': event.id}


@app.post('/api/meetings/{meeting_id}/exports')
def export(meeting_id: str, body: dict, user=Depends(current_user)):
    if body.get('format') not in ('pdf', 'docx'):
        raise HTTPException(422, 'Выберите pdf или docx')
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id)
        if meeting.state != 'confirmed':
            raise HTTPException(409, 'Подтвердите протокол')
        event = jobs.emit(db, 'export.requested', meeting, user.id, {'format': body['format']})
        return {'event_id': event.id}


@app.get('/api/exports/{job_id}')
def download(job_id: str, user=Depends(current_user)):
    with Session() as db:
        job = db.get(Job, job_id)
        if not job or job.kind != 'export' or job.state != 'succeeded':
            raise HTTPException(404, 'Файл не готов')
        meeting = require_meeting(db, job.meeting_id, user.id)
        if meeting.confirmed_revision != job.revision:
            raise HTTPException(409, 'Запросите экспорт актуальной версии')
        return FileResponse(settings.data_dir / 'exports' / job.result['filename'], filename=f'protocol-{job.revision}.{job.payload["format"]}')


@app.delete('/api/meetings/{meeting_id}')
def delete(meeting_id: str, user=Depends(current_user)):
    with Session.begin() as db:
        meeting = require_meeting(db, meeting_id, user.id, write=True, lock=True)
        cancel_old(db, meeting)
        meeting.deleted = True
        jobs.emit(db, 'meeting.deleted', meeting, user.id)
        return {'deleted': True}


@app.get('/api/notifications')
def notifications(user=Depends(current_user)):
    with Session() as db:
        items = []
        for n in db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.created.desc())):
            try:
                require_meeting(db, n.meeting_id, user.id)
                items.append({'id': n.key, 'text': n.text, 'meeting_id': n.meeting_id})
            except HTTPException:
                continue
        return items


@app.post('/api/references')
def reference(body: dict, user=Depends(current_user)):
    title, value = str(body.get('title', '')).strip(), str(body.get('text', '')).strip()
    if not title or not value or len(value) > 50000:
        raise HTTPException(422, 'Укажите название и текст до 50000 символов')
    with Session.begin() as db:
        ref = Reference(user_id=user.id, title=title[:300], text=value)
        db.add(ref)
        db.flush()
        jobs.emit(db, 'document.approved', None, user.id, {'reference_id': ref.id})
        return {'id': ref.id}


@app.post('/internal/events/accept', dependencies=internal)
def accept(body: dict):
    with Session() as db:
        event = db.get(Event, body.get('event_id'))
        if not event:
            raise HTTPException(404, 'Unknown outbox event')
        return {'event_id': event.id, 'event_type': event.kind, 'revision': event.revision, 'meeting_id': event.meeting_id}


@app.post('/internal/events/dispatch', dependencies=internal)
@app.post('/internal/speech-jobs', dependencies=internal)
@app.post('/internal/analysis-runs', dependencies=internal)
@app.post('/internal/question-runs/dispatch', dependencies=internal)
@app.post('/internal/rag/index-jobs', dependencies=internal)
@app.post('/internal/export-jobs', dependencies=internal)
@app.post('/internal/deletion-jobs', dependencies=internal)
def dispatch(body: dict):
    return jobs.dispatch_event(body['event_id'])


@app.post('/internal/work/claim', dependencies=internal)
def claim():
    return jobs.claim(jobs.AI_KINDS)


@app.post('/internal/work/context', dependencies=internal)
def work_context(body: WorkInput):
    return jobs.context(body.scope_token)


@app.post('/internal/work/validate', dependencies=internal)
def work_validate(body: WorkInput):
    with Session() as db:
        job = scoped_job(db, body.scope_token)
        try:
            value = jobs.validate(db, job, body.output)
            return {'valid': True, 'scope_token': body.scope_token, 'output': value}
        except (ValueError, ValidationError, KeyError, TypeError) as error:
            # Only schema/validation information; no arbitrary exception dumps to the UI.
            return {'valid': False, 'scope_token': body.scope_token, 'output': body.output, 'repair_error': str(error)[:1600]}


@app.post('/internal/work/repair', dependencies=internal)
def repair(body: dict):
    ctx = jobs.context(body['scope_token'])
    r = httpx.post(f'{settings.ollama_url}/api/chat', timeout=180, json={
        'model': settings.llm_model, 'stream': False, 'think': False, 'format': ctx['schema'],
        'options': {'temperature': 0, 'num_ctx': 8192, 'num_predict': 1800},
        'messages': [{'role': 'system', 'content': ctx['system_prompt']},
                     {'role': 'user', 'content': ctx['prompt_input'] + '\nИсправь предыдущий JSON: ' + json.dumps(body.get('output'), ensure_ascii=False) + '\nОшибка: ' + body.get('repair_error', '')}]})
    r.raise_for_status()
    return {'scope_token': body['scope_token'], 'output': r.json()['message']['content']}


@app.post('/internal/work/commit', dependencies=internal)
def work_commit(body: WorkInput):
    try:
        return jobs.commit(body.scope_token, body.output)
    except (ValueError, ValidationError, KeyError, TypeError):
        raise HTTPException(422, 'Invalid model output')


@app.post('/internal/work/fail', dependencies=internal)
def work_fail(body: dict):
    return jobs.fail(body['scope_token'], str(body.get('code', 'workflow_failed')))


@app.post('/internal/tools/{name}', dependencies=internal)
def tool(name: str, body: dict):
    try:
        return tools.execute(name, body.get('scope_token'), body.get('arguments_json', '{}'))
    except (ValueError, ValidationError):
        raise HTTPException(422, 'Invalid tool arguments')


@app.post('/internal/reminders/scan', dependencies=internal)
def reminders():
    from datetime import datetime, timedelta
    count = 0
    with Session.begin() as db:
        for meeting in db.scalars(select(Meeting).where(Meeting.state == 'confirmed', Meeting.deleted == False)):
            today = datetime.now(ZoneInfo(meeting.document.get('timezone', 'Asia/Qyzylorda'))).date()
            for action in meeting.document.get('actions', []):
                due = action.get('due', {}).get('date')
                if not due or action.get('status') == 'done':
                    continue
                due_date = date.fromisoformat(due)
                kind = 'overdue' if due_date < today else ('approaching' if due_date <= today+timedelta(days=1) else None)
                if not kind:
                    continue
                key = f'{action["id"]}:{due}:{kind}:{meeting.owner_id}'
                if not db.get(Notification, key):
                    db.add(Notification(key=key, user_id=meeting.owner_id, meeting_id=meeting.id, text=f'{"Просрочено" if kind == "overdue" else "Срок приближается"}: {action["title"]} — {due}'))
                    count += 1
    return {'created_count': count}


@app.post('/internal/maintenance/reconcile', dependencies=internal)
def maintenance():
    return jobs.reconcile()


@app.post('/internal/workflow-errors', dependencies=internal)
def workflow_errors(body: dict):
    # Failure branches release known leases. Unknown failures recover by lease timeout.
    metadata = {key: str(body.get(key, ''))[:100] for key in ('execution_id', 'workflow_id')}
    with Session.begin() as db:
        db.add(Event(kind='workflow.error', user_id='system', state='recorded', payload=metadata))
    return {'recorded': True, **metadata}


@app.post('/internal/smoke/validate-model-results', dependencies=internal)
def smoke_validate(body: dict):
    from .rag import embed
    vectors = embed(['Айдана подготовит отчёт.', 'Айдана есеп дайындайды.'])
    return {'ok': True, 'embedding_dimensions': len(vectors[0]), 'model': settings.llm_model}


@app.post('/internal/smoke/finish', dependencies=internal)
def smoke_finish(body: dict):
    steps = body.get('intermediateSteps', [])
    called = any(s.get('action', {}).get('tool') == 'smoke_echo' for s in steps)
    if not called:
        raise HTTPException(422, 'No observed real tool call')
    return {'ok': True, 'real_tool_call': True, 'model': settings.llm_model}


dist = Path('/app/frontend/dist')
if dist.exists():
    app.mount('/', StaticFiles(directory=dist, html=True), name='web')
