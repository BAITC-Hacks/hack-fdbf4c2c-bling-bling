from sqlalchemy import ForeignKey, String, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MeetingAudio(Base):
    __tablename__ = "meeting_audio"

    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    original_path: Mapped[str] = mapped_column(String)
    wav_path: Mapped[str] = mapped_column(String)
    duration_seconds: Mapped[float] = mapped_column(Float)
