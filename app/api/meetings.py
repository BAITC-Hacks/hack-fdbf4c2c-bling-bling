from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.repositories.meetings import create_meeting, find_meeting
from app.schemas.meeting import MeetingCreate, MeetingRead, MeetingStatus

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
