import hashlib
import hmac
import secrets
import time
from fastapi import HTTPException, Request
from sqlalchemy import select
from .config import settings
from .db import Access, Job, LoginSession, Meeting, Session, User


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value: str):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 310000).hex()
    return f'{salt}:{key}'


def password_valid(value: str, stored: str):
    salt, expected = stored.split(':')
    actual = hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 310000).hex()
    return hmac.compare_digest(actual, expected)


def current_user(request: Request):
    token = request.cookies.get('hackalem_session', '')
    with Session() as db:
        session = db.get(LoginSession, digest(token))
        if not session or session.expires < time.time():
            raise HTTPException(401, 'Войдите в систему')
        user = db.get(User, session.user_id)
        if not user:
            raise HTTPException(401, 'Сессия недействительна')
        # Same-origin JSON/form requests; reject foreign browser origins.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD') and origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Недопустимый Origin')
        return user


def service_auth(request: Request):
    expected = f'Bearer {settings.service_token}'
    if not hmac.compare_digest(request.headers.get('authorization', ''), expected):
        raise HTTPException(401, 'Service authentication required')


def require_meeting(db, meeting_id, user_id, write=False):
    meeting = db.get(Meeting, meeting_id)
    access = db.scalar(select(Access).where(Access.meeting_id == meeting_id, Access.user_id == user_id))
    allowed = meeting and not meeting.deleted and (meeting.owner_id == user_id or (access and (not write or access.role == 'editor')))
    if not allowed:
        raise HTTPException(403, 'Нет доступа к совещанию')
    return meeting


def scope_token(job):
    content = f'{job.id}.{job.lease}'
    sig = hmac.new(settings.app_secret.encode(), content.encode(), hashlib.sha256).hexdigest()
    return f'{content}.{sig}'


def scoped_job(db, token, lock=False):
    try:
        job_id, lease, signature = token.split('.')
    except (AttributeError, ValueError):
        raise HTTPException(403, 'Invalid scope')
    query = select(Job).where(Job.id == job_id)
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if not job or not job.lease or not hmac.compare_digest(scope_token(job), token):
        raise HTTPException(403, 'Invalid scope')
    if job.state != 'running' or job.expires < time.time():
        raise HTTPException(409, 'Expired lease')
    if job.meeting_id:
        meeting = require_meeting(db, job.meeting_id, job.user_id)
        if meeting.revision != job.revision:
            raise HTTPException(409, 'Stale revision')
    return job
