"""Diarization and manual speaker names, scoped to one meeting."""
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.meetings import require_meeting
from app.db.database import get_session
from app.models.audio import MeetingAudio
from app.models.diarization import MeetingDiarization
from app.schemas.diarization import DiarizationSegment, SpeakerMapping, SpeakerRead
from app.services.diarization import DiarizationError, DiarizationService

router = APIRouter(prefix="/meetings", tags=["diarization"])
DatabaseSession = Annotated[Session, Depends(get_session)]
segments_adapter = TypeAdapter(list[DiarizationSegment])


def get_diarization_service(request: Request) -> DiarizationService:
    return request.app.state.diarization_service


def diarization_path(request: Request, meeting_id: UUID) -> Path:
    return request.app.state.settings.data_dir / "processed" / str(meeting_id) / "diarization.json"


def require_result(session: Session, meeting_id: UUID) -> MeetingDiarization:
    require_meeting(session, meeting_id)
    record = session.get(MeetingDiarization, str(meeting_id))
    if record is None or record.status != "ready":
        raise HTTPException(409, detail={"code": "diarization_not_ready", "message": "Диаризация ещё не завершена."})
    return record


def read_result(request: Request, meeting_id: UUID) -> list[DiarizationSegment]:
    try:
        return segments_adapter.validate_json(diarization_path(request, meeting_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(500, detail={"code": "diarization_unavailable", "message": "Локальный результат диаризации отсутствует или повреждён."}) from None


@router.post("/{id}/diarize", response_model=list[DiarizationSegment])
def diarize(id: UUID, request: Request, session: DatabaseSession,
            service: Annotated[DiarizationService, Depends(get_diarization_service)]):
    require_meeting(session, id)
    audio = session.get(MeetingAudio, str(id))
    if audio is None:
        raise HTTPException(409, detail={"code": "audio_not_ready", "message": "Сначала загрузите и подготовьте аудио."})
    # Unique PK makes the claim atomic, including across server processes.
    record = MeetingDiarization(meeting_id=str(id), status="running", speaker_names={})
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail={"code": "diarization_conflict", "message": "Диаризация уже запущена или завершена."}) from None
    target = diarization_path(request, id)
    temporary = target.with_name(f".{uuid4().hex}.tmp")
    written = completed = False
    try:
        result = segments_adapter.validate_python(service.diarize(Path(audio.wav_path)))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(segments_adapter.dump_json(result, indent=2))
        temporary.replace(target)
        written = True
        record.status = "ready"
        session.commit()
        completed = True
        return result
    except DiarizationError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    except OSError:
        raise HTTPException(507, detail={"code": "diarization_storage_error", "message": "Не удалось сохранить диаризацию локально."}) from None
    finally:
        if not completed:
            session.rollback()
            try:
                temporary.unlink(missing_ok=True)
                if written:
                    target.unlink(missing_ok=True)
            finally:
                session.delete(record)
                session.commit()


@router.get("/{id}/diarization", response_model=list[DiarizationSegment])
def get_diarization(id: UUID, request: Request, session: DatabaseSession):
    require_result(session, id)
    return read_result(request, id)


@router.get("/{id}/diarization/status")
def get_status(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingDiarization, str(id))
    return {"id": str(id), "status": record.status if record else "not_started"}


@router.get("/{id}/speakers", response_model=list[SpeakerRead])
def get_speakers(id: UUID, request: Request, session: DatabaseSession):
    record = require_result(session, id)
    speaker_ids = sorted({segment.speaker_id for segment in read_result(request, id)})
    return [SpeakerRead(speaker_id=key, name=record.speaker_names.get(key)) for key in speaker_ids]


@router.put("/{id}/speakers", response_model=list[SpeakerRead])
def set_speakers(id: UUID, mapping: SpeakerMapping, request: Request, session: DatabaseSession):
    record = require_result(session, id)
    speaker_ids = {segment.speaker_id for segment in read_result(request, id)}
    if mapping.names.keys() - speaker_ids:
        raise HTTPException(422, detail={"code": "unknown_speaker", "message": "В сопоставлении есть голоса, которых нет в этой встрече."})
    # PUT replaces all manual assignments. Omitted speakers become unnamed.
    record.speaker_names = dict(mapping.names)
    session.commit()
    return [SpeakerRead(speaker_id=key, name=record.speaker_names.get(key)) for key in sorted(speaker_ids)]
