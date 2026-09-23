import json
from fastapi import HTTPException
from .contracts import ROLE_TOOLS, ToolArgs
from .db import Meeting, Session, ToolAudit
from .deadlines import normalize
from .security import scoped_job


def execute(name, token, arguments):
    args = ToolArgs.model_validate(json.loads(arguments) if isinstance(arguments, str) else arguments)
    with Session.begin() as db:
        job = scoped_job(db, token, lock=True)
        if name not in ROLE_TOOLS.get(job.kind, []):
            raise HTTPException(403, 'Tool not allowed for role')
        if job.tool_calls >= 6:
            raise HTTPException(429, 'Tool budget exhausted')
        job.tool_calls += 1
        db.add(ToolAudit(job_id=job.id, tool=name))
        meeting = db.get(Meeting, job.meeting_id)
        doc = meeting.document
        segments = {s['id']: s for s in doc.get('segments', [])}
        ids = args.segment_ids or args.source_segment_ids or args.context_segment_ids
        if any(i not in segments for i in ids):
            raise HTTPException(403, 'Source unavailable')
        if name == 'get_evidence':
            return {'segments': [segments[i] for i in ids]}
        if name == 'resolve_participant':
            name_lower = (args.mentioned_name or '').casefold()
            return {'candidates': [p for p in doc.get('participants', []) if name_lower and name_lower in p.casefold()], 'requires_confirmation': True}
        if name == 'normalize_deadline':
            evidence = ' '.join(segments[i]['text'] for i in ids).casefold()
            if args.due_raw and args.due_raw.casefold() not in evidence:
                return {'date': None, 'needs_review': True, 'note': 'Фраза не найдена в источнике'}
            return normalize(args.due_raw, doc.get('started_at'), doc.get('timezone', 'Asia/Qyzylorda'))
        if name == 'get_action_candidates':
            candidates = doc.get('candidates', [])
            return {'candidates': [a for a in candidates if not args.candidate_ids or a['id'] in args.candidate_ids][:20]}
        if name == 'get_confirmed_actions':
            if meeting.confirmed_revision != job.revision:
                return {'actions': []}
            return {'actions': [a for a in doc.get('actions', []) if (args.status_filter == 'all' or a['status'] == args.status_filter) and (not args.assignee_name or a.get('assignee_mention') == args.assignee_name)][:30]}
        if name in ('search_reference', 'search_meetings'):
            from .rag import search
            return {'evidence': search(db, job, args.query, args.limit, reference=name == 'search_reference')}
        raise HTTPException(404, 'Unknown tool')
