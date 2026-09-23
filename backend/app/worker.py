import copy
import json
import shutil
import threading
import time
from pathlib import Path
import httpx
from sqlalchemy import select, delete
from . import jobs
from .config import settings
from .db import Event, Job, Meeting, Notification, Reference, Session, Snapshot, initialize


def deliver_events():
    with Session() as db:
        pending = list(db.scalars(select(Event).where(Event.state == 'pending', Event.next_attempt <= time.time()).limit(10)))
    for event in pending:
        try:
            response = httpx.post(f'{settings.n8n_url}/webhook/meeting-events', headers={'X-Hackalem-Event': settings.webhook_token},
                                  json={'schema_version': 1, 'event_id': event.id, 'event_type': event.kind, 'meeting_id': event.meeting_id, 'revision': event.revision}, timeout=15)
            response.raise_for_status()
        except Exception:
            with Session.begin() as db:
                record = db.get(Event, event.id)
                if record and record.state == 'pending':
                    record.attempts += 1
                    record.next_attempt = time.time() + min(60, 2**min(record.attempts, 6))


def heartbeat(job_id, token, stopped):
    while not stopped.wait(20):
        with Session.begin() as db:
            job = db.get(Job, job_id)
            if not job or job.state != 'running' or job.lease != token:
                return
            job.expires = time.time() + settings.lease_seconds


def process(claimed):
    with Session() as db:
        job = db.get(Job, claimed['work_unit_id'])
        lease = job.lease
    stopped = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(job.id, lease, stopped), daemon=True)
    thread.start()
    try:
        if job.kind == 'speech':
            from .speech import transcribe
            result = transcribe(settings.data_dir / 'media' / job.meeting_id / job.payload['filename'])
        elif job.kind in ('index', 'reference_index'):
            from .rag import index_document
            with Session() as db:
                result = index_document(db, job)
        elif job.kind == 'export':
            from .exports import export_document
            with Session() as db:
                meeting = db.get(Meeting, job.meeting_id)
                snapshot = db.scalar(select(Snapshot).where(Snapshot.meeting_id == job.meeting_id, Snapshot.revision == job.revision))
                if not snapshot:
                    raise ValueError('snapshot_missing')
                result = export_document(meeting.title, snapshot.document, job.id, job.payload['format'])
        elif job.kind == 'delete':
            from .rag import delete_meeting
            delete_meeting(job.meeting_id)
            media_dir = (settings.data_dir / 'media' / job.meeting_id).resolve()
            if settings.data_dir.resolve() not in media_dir.parents:
                raise ValueError('invalid_storage_path')
            if media_dir.exists():
                shutil.rmtree(media_dir)
            with Session.begin() as db:
                for export_job in db.scalars(select(Job).where(Job.meeting_id == job.meeting_id, Job.kind == 'export')):
                    (settings.data_dir / 'exports' / f'{export_job.id}.{export_job.payload.get("format", "pdf")}').unlink(missing_ok=True)
                db.execute(delete(Snapshot).where(Snapshot.meeting_id == job.meeting_id))
                db.execute(delete(Notification).where(Notification.meeting_id == job.meeting_id))
                meeting = db.get(Meeting, job.meeting_id)
                meeting.document = {}
                for old in db.scalars(select(Job).where(Job.meeting_id == job.meeting_id, Job.id != job.id)):
                    old.payload, old.result = {}, {}
            result = {'deleted': True}
        else:
            raise ValueError('unsupported_job')
        with Session.begin() as db:
            if job.meeting_id:
                db.scalar(select(Meeting).where(Meeting.id == job.meeting_id).with_for_update())
            active = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
            meeting = db.get(Meeting, job.meeting_id) if job.meeting_id else None
            if active.state != 'running' or active.lease != lease or active.expires < time.time() or (meeting and (meeting.revision != job.revision or (meeting.deleted and job.kind != 'delete'))):
                return
            active.state, active.result = 'succeeded', result
            if job.kind == 'speech':
                meeting.document = {**meeting.document, **result, 'input_mode': 'audio', 'actions': [], 'summary': []}
                jobs.emit(db, 'transcript.ready', meeting, job.user_id)
            elif job.kind == 'index':
                meeting.indexed_revision = job.revision
            elif job.kind == 'reference_index':
                db.get(Reference, job.payload['reference_id']).indexed = True
        print(json.dumps({'job_id': job.id, 'kind': job.kind, 'state': 'succeeded'}), flush=True)
    except Exception as error:
        with Session.begin() as db:
            active = db.get(Job, job.id)
            if active and active.lease == lease and active.state == 'running':
                active.error = type(error).__name__
                active.state = 'failed'
        print(json.dumps({'job_id': job.id, 'kind': job.kind, 'state': 'failed', 'code': type(error).__name__}), flush=True)
    finally:
        stopped.set()


def main():
    initialize()
    while True:
        try:
            deliver_events()
            work = jobs.claim(jobs.WORKER_KINDS)
            if work['has_work']:
                process(work)
        except Exception as error:
            print(json.dumps({'worker_error': type(error).__name__}), flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
