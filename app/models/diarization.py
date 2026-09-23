from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MeetingDiarization(Base):
    """Independent stage state; manual names belong only to this meeting."""

    __tablename__ = "meeting_diarization"

    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    speaker_names: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
