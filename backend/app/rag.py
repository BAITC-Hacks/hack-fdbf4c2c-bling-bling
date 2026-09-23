import hashlib
import math
import uuid
import httpx
from qdrant_client import QdrantClient, models
from sqlalchemy import select
from .config import settings
from .db import Meeting, Reference, Snapshot
from .security import require_meeting


def client():
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=60, check_compatibility=False)


def embed(texts):
    with httpx.Client(timeout=180) as http:
        response = http.post(f'{settings.ollama_url}/api/embed', json={
            'model': settings.embedding_model, 'input': texts, 'truncate': False,
            'keep_alive': '0s', 'options': {'num_gpu': 0}})
        response.raise_for_status()
        vectors = response.json()['embeddings']
    if len(vectors) != len(texts) or not vectors or any(not v or not all(math.isfinite(x) for x in v) or sum(x*x for x in v) == 0 for v in vectors):
        raise ValueError('Invalid embeddings')
    return vectors


def collection(dimension):
    profile = hashlib.sha256(settings.embedding_model.encode()).hexdigest()[:10]
    return f'hackalem_{profile}_{dimension}'


def ensure_collection(qdrant, size):
    name = collection(size)
    if not qdrant.collection_exists(name):
        qdrant.create_collection(name, vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
        for key in ('meeting_id', 'revision', 'reference_id', 'owner_id', 'kind'):
            qdrant.create_payload_index(name, key, field_schema=models.PayloadSchemaType.INTEGER if key == 'revision' else models.PayloadSchemaType.KEYWORD)
    return name


def index_document(db, job):
    if job.kind == 'reference_index':
        ref = db.get(Reference, job.payload['reference_id'])
        if not ref or ref.user_id != job.user_id:
            raise ValueError('Reference unavailable')
        entries = [{'text': ref.text[i:i+1600], 'reference_id': ref.id, 'owner_id': ref.user_id, 'kind': 'reference', 'title': ref.title} for i in range(0, len(ref.text), 1400)]
    else:
        meeting = require_meeting(db, job.meeting_id, job.user_id)
        snapshot = db.scalar(select(Snapshot).where(Snapshot.meeting_id == meeting.id, Snapshot.revision == job.revision))
        if not snapshot or meeting.confirmed_revision != job.revision:
            raise ValueError('Confirmed snapshot required')
        entries = [{'text': s['text'], 'segment_ids': [s['id']], 'start_ms': s['start_ms'], 'meeting_id': meeting.id,
                    'revision': job.revision, 'kind': 'transcript'} for s in snapshot.document['segments']]
        entries += [{'text': f"{a['title']}; исполнитель: {a.get('assignee_mention')}; срок: {a.get('due', {}).get('date') or a.get('due_raw')}",
                     'segment_ids': a['source_segment_ids'], 'meeting_id': meeting.id, 'revision': job.revision, 'kind': 'action'} for a in snapshot.document.get('actions', [])]
    qdrant = client()
    for offset in range(0, len(entries), 4):
        batch = entries[offset:offset+4]
        vectors = embed([e['text'] for e in batch])
        name = ensure_collection(qdrant, len(vectors[0]))
        points = [models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f'{job.key}:{offset+i}:{settings.embedding_model}')),
                                     vector=vector, payload=entry) for i, (vector, entry) in enumerate(zip(vectors, batch))]
        qdrant.upsert(name, points=points, wait=True)
    # Publication happens in the worker transaction after lease/revision recheck.
    return {'indexed_chunks': len(entries)}


def search(db, job, query, limit=6, reference=False):
    if not query or len(query) > 1000:
        return []
    if reference:
        refs = list(db.scalars(select(Reference).where(Reference.user_id == job.user_id, Reference.indexed == True)))
        if not refs:
            return []
        conditions = [models.FieldCondition(key='reference_id', match=models.MatchAny(any=[r.id for r in refs]))]
    else:
        meeting = require_meeting(db, job.meeting_id, job.user_id)
        if meeting.indexed_revision != job.revision or meeting.confirmed_revision != job.revision:
            return []
        conditions = [models.FieldCondition(key='meeting_id', match=models.MatchValue(value=meeting.id)),
                      models.FieldCondition(key='revision', match=models.MatchValue(value=job.revision))]
    vector = embed([query])[0]
    qdrant = client()
    name = collection(len(vector))
    if not qdrant.collection_exists(name):
        return []
    points = qdrant.query_points(name, query=vector, query_filter=models.Filter(must=conditions), limit=min(limit, 6), with_payload=True).points
    # Recheck the source of truth before returning any text.
    if not reference:
        db.expire(meeting)
        require_meeting(db, meeting.id, job.user_id)
        if meeting.revision != job.revision or meeting.indexed_revision != job.revision:
            return []
    return [{**p.payload, 'score': p.score} for p in points]


def delete_meeting(meeting_id):
    qdrant = client()
    for col in qdrant.get_collections().collections:
        if col.name.startswith('hackalem_'):
            qdrant.delete(col.name, points_selector=models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(key='meeting_id', match=models.MatchValue(value=meeting_id))])), wait=True)
