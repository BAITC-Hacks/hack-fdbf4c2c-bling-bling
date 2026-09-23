"""Synchronous local transcription of an already prepared meeting."""
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.meetings import require_meeting
from app.db.database import get_session
from app.models.audio import MeetingAudio
from app.models.meeting import Meeting
from app.schemas.transcript import TranscriptRaw
from app.services.transcription import TranscriptionError, TranscriptionService

router = APIRouter(prefix="/meetings", tags=["transcription"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def get_transcription_service(request: Request) -> TranscriptionService:
    return request.app.state.transcription_service


def transcript_path(request: Request, meeting_id: UUID) -> Path:
    return request.app.state.settings.data_dir / "processed" / str(meeting_id) / "transcript_raw.json"


@router.post("/{id}/transcribe", response_model=TranscriptRaw)
def transcribe(id: UUID, request: Request, session: DatabaseSession,
               service: Annotated[TranscriptionService, Depends(get_transcription_service)]):
    require_meeting(session, id)
    audio = session.get(MeetingAudio, str(id))
    if audio is None:
        raise HTTPException(409, detail={"code": "audio_not_ready", "message": "Сначала загрузите и подготовьте аудио."})
    claimed = session.execute(update(Meeting).where(
        Meeting.id == str(id), Meeting.status == "audio_ready").values(status="transcribing"))
    session.commit()
    if not claimed.rowcount:
        raise HTTPException(409, detail={"code": "transcription_conflict", "message": "Распознавание уже запущено или завершено."})
    target = transcript_path(request, id)
    temporary = target.with_name(f".{uuid4().hex}.tmp")
    written = False
    completed = False
    try:
        result = service.transcribe(Path(audio.wav_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        written = True
        session.execute(update(Meeting).where(Meeting.id == str(id)).values(status="transcribed"))
        session.commit()
        completed = True
        return result
    except TranscriptionError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    except OSError:
        raise HTTPException(507, detail={"code": "transcript_storage_error", "message": "Не удалось сохранить транскрипт локально."}) from None
    finally:
        if not completed:
            session.rollback()
            try:
                temporary.unlink(missing_ok=True)
                if written:
                    target.unlink(missing_ok=True)
            finally:
                session.execute(update(Meeting).where(Meeting.id == str(id)).values(status="audio_ready"))
                session.commit()


@router.get("/{id}/transcript", response_model=TranscriptRaw)
def get_transcript(id: UUID, request: Request, session: DatabaseSession):
    meeting = require_meeting(session, id)
    if meeting.status != "transcribed":
        raise HTTPException(409, detail={"code": "transcript_not_ready", "message": "Транскрипт ещё не готов."})
    try:
        return TranscriptRaw.model_validate_json(transcript_path(request, id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(500, detail={"code": "transcript_unavailable", "message": "Сохранённый транскрипт отсутствует или повреждён."}) from None
