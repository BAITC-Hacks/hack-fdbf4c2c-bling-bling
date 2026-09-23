import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.action_items import get_action_items
from app.api.meetings import require_meeting
from app.api.transcripts import get_transcript
from app.db.database import get_session
from app.models.summary import MeetingSummaryRecord
from app.schemas.action_item import ActionItemExtractionResult
from app.schemas.summary import MeetingSummaryRead
from app.services.local_llm import LocalLLMError
from app.services.summarization import MeetingSummaryService, SummaryError

router = APIRouter(prefix="/meetings", tags=["summary"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def get_summary_service(request: Request) -> MeetingSummaryService:
    return request.app.state.summary_service


@router.post("/{id}/summarize", response_model=MeetingSummaryRead)
def summarize(id: UUID, request: Request, session: DatabaseSession,
              service: Annotated[MeetingSummaryService, Depends(get_summary_service)]):
    transcript = get_transcript(id, request, session)
    extraction = get_action_items(id, session)
    transcript_hash = hashlib.sha256(transcript.model_dump_json().encode("utf-8")).hexdigest()
    if transcript_hash != extraction.source_transcript_sha256:
        raise HTTPException(409, detail={"code": "summary_source_mismatch", "message": "Транскрипт изменён после извлечения поручений. Нельзя смешивать разные версии источников."})
    actions = ActionItemExtractionResult(action_items=extraction.action_items)
    record = MeetingSummaryRecord(meeting_id=str(id), status="running")
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail={"code": "summary_conflict", "message": "Summary уже формируется или сохранено."}) from None
    completed = False
    try:
        result = service.summarize(extraction.source_segments, actions)
        saved = MeetingSummaryRead(**result.model_dump(), source_segments=extraction.source_segments,
                                   source_transcript_sha256=transcript_hash,
                                   source_action_items_sha256=hashlib.sha256(actions.model_dump_json().encode("utf-8")).hexdigest(),
                                   model=request.app.state.settings.ollama_model)
        record.result = saved.model_dump(mode="json")
        record.status = "ready"
        session.commit()
        completed = True
        return saved
    except (SummaryError, LocalLLMError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    finally:
        if not completed:
            session.rollback()
            session.delete(record)
            session.commit()


@router.get("/{id}/summary", response_model=MeetingSummaryRead)
def get_summary(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingSummaryRecord, str(id))
    if record is None or record.status != "ready":
        raise HTTPException(409, detail={"code": "summary_not_ready", "message": "Summary ещё не готово."})
    try:
        return MeetingSummaryRead.model_validate(record.result)
    except ValueError:
        raise HTTPException(500, detail={"code": "summary_unavailable", "message": "Сохранённое summary повреждено."}) from None


@router.get("/{id}/summary/status")
def summary_status(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingSummaryRecord, str(id))
    return {"id": str(id), "status": record.status if record else "not_started"}
