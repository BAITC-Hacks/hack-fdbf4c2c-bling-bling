from typing import Annotated
from uuid import UUID
from uuid import uuid4
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.repositories.meetings import create_meeting, find_meeting
from app.schemas.meeting import MeetingCreate, MeetingRead, MeetingStatus
from app.models.meeting import Meeting
from app.models.audio import MeetingAudio
from app.schemas.audio import AudioRead
from app.services.audio import AudioError, FORMATS, save_upload, convert_audio

router = APIRouter(prefix="/meetings", tags=["meetings"])
DatabaseSession = Annotated[Session, Depends(get_session)]


@router.post("", response_model=MeetingRead, status_code=201)
def create(payload: MeetingCreate, session: DatabaseSession):
    return create_meeting(session, payload)


def require_meeting(session: Session, meeting_id: UUID):
    meeting = find_meeting(session, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.get("/{id}", response_model=MeetingRead)
def get_meeting(id: UUID, session: DatabaseSession):
    return require_meeting(session, id)


@router.get("/{id}/status", response_model=MeetingStatus)
def get_status(id: UUID, session: DatabaseSession):
    return require_meeting(session, id)


@router.post("/{id}/upload", response_model=AudioRead)
def upload(id: UUID, request: Request, session: DatabaseSession,
           file: Annotated[UploadFile, File()]):
    require_meeting(session, id)
    extension = Path(file.filename or "").suffix.lower()
    if extension not in FORMATS:
        raise HTTPException(415, detail={"code": "unsupported_format", "message": "Поддерживаются wav, mp3, m4a, mp4 и webm."})
    # Atomic claim prevents simultaneous uploads from overwriting each other.
    claimed = session.execute(update(Meeting).where(Meeting.id == str(id), Meeting.status == "created")
                              .values(status="uploading"))
    session.commit()
    if not claimed.rowcount:
        raise HTTPException(409, detail={"code": "upload_conflict", "message": "Загрузка уже выполняется или аудио уже подготовлено."})
    settings = request.app.state.settings
    token = uuid4().hex
    source = settings.data_dir / "uploads" / f"{id}_{token}{extension}"
    output = settings.data_dir / "processed" / f"{id}_{token}.wav"
    completed = False
    try:
        save_upload(file.file, source, settings.max_upload_bytes)
        duration = convert_audio(source, output, settings)
        session.add(MeetingAudio(meeting_id=str(id), original_path=str(source),
                                 wav_path=str(output), duration_seconds=duration))
        session.execute(update(Meeting).where(Meeting.id == str(id)).values(status="audio_ready"))
        session.commit()
        completed = True
        return AudioRead(id=id, duration_seconds=duration)
    except AudioError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    except OSError:
        raise HTTPException(507, detail={"code": "storage_error", "message": "Не удалось сохранить аудио локально."}) from None
    finally:
        file.file.close()
        if not completed:
            session.rollback()
            source.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            session.execute(update(Meeting).where(Meeting.id == str(id)).values(status="created"))
            session.commit()
