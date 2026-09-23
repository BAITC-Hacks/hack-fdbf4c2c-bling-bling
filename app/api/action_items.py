import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.meetings import require_meeting
from app.api.transcripts import get_transcript
from app.db.database import get_session
from app.models.action_item import MeetingExtraction
from app.schemas.action_item import ActionItemExtractionRead, ExtractionSegment
from app.services.extraction import ActionItemExtractionService, ExtractionError
from app.services.local_llm import LocalLLMError

router = APIRouter(prefix="/meetings", tags=["action-items"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def get_extraction_service(request: Request) -> ActionItemExtractionService:
    return request.app.state.extraction_service


@router.post("/{id}/extract-action-items", response_model=ActionItemExtractionRead)
def extract_action_items(id: UUID, request: Request, session: DatabaseSession,
                         service: Annotated[ActionItemExtractionService, Depends(get_extraction_service)]):
    transcript = get_transcript(id, request, session)
    try:
        segments = [ExtractionSegment(id=s.id, text=s.text) for s in transcript.segments if s.text.strip()]
    except ValueError:
        raise HTTPException(500, detail={"code": "extraction_source_invalid", "message": "Сохранённые сегменты не подходят для извлечения поручений."}) from None
    record = MeetingExtraction(meeting_id=str(id), status="running")
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail={"code": "extraction_conflict", "message": "Извлечение уже запущено или завершено."}) from None
    completed = False
    try:
        result = service.extract(segments)
        saved = ActionItemExtractionRead(
            action_items=result.action_items, source_segments=segments,
            source_transcript_sha256=hashlib.sha256(transcript.model_dump_json().encode("utf-8")).hexdigest(),
            model=request.app.state.settings.ollama_model,
        )
        # One SQLite transaction saves both final output and its immutable sources.
        record.result = saved.model_dump(mode="json")
        record.status = "ready"
        session.commit()
        completed = True
        return saved
    except (ExtractionError, LocalLLMError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    finally:
        if not completed:
            session.rollback()
            session.delete(record)
            session.commit()


@router.get("/{id}/action-items", response_model=ActionItemExtractionRead)
def get_action_items(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingExtraction, str(id))
    if record is None or record.status != "ready":
        raise HTTPException(409, detail={"code": "action_items_not_ready", "message": "Поручения ещё не извлечены."})
    try:
        return ActionItemExtractionRead.model_validate(record.result)
    except ValueError:
        raise HTTPException(500, detail={"code": "action_items_unavailable", "message": "Сохранённые поручения повреждены."}) from None


@router.get("/{id}/action-items/status")
def extraction_status(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingExtraction, str(id))
    return {"id": str(id), "status": record.status if record else "not_started"}
