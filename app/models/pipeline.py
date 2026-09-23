from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class MeetingPipeline(Base):
    __tablename__ = "meeting_pipelines"
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str] = mapped_column(String(30))
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    exports: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MeetingAnalysisRecord(Base):
    __tablename__ = "meeting_analyses"
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id"), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    alignment: Mapped[list] = mapped_column(JSON)
