from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MeetingExtraction(Base):
    __tablename__ = "meeting_extractions"

    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
