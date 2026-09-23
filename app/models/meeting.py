from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="created")
    # SQLite stores this timestamp without timezone; it is always UTC.
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    details: Mapped["MeetingDetails | None"] = relationship(cascade="all, delete-orphan", uselist=False)

    @property
    def meeting_date(self) -> date | None:
        return self.details.meeting_date if self.details else None


class MeetingDetails(Base):
    """Optional metadata in a new table, preserving existing SQLite meeting rows."""
    __tablename__ = "meeting_details"
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    meeting_date: Mapped[date] = mapped_column(Date)
