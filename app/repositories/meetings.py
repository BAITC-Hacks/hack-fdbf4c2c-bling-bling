from uuid import UUID

from sqlalchemy.orm import Session

from app.models.meeting import Meeting
from app.schemas.meeting import MeetingCreate


def create_meeting(session: Session, payload: MeetingCreate) -> Meeting:
    meeting = Meeting(title=payload.title)
    session.add(meeting)
    session.commit()
    session.refresh(meeting)
    return meeting


def find_meeting(session: Session, meeting_id: UUID) -> Meeting | None:
    return session.get(Meeting, str(meeting_id))
