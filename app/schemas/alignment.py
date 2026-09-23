from pydantic import Field

from app.schemas.action_item import ExtractionSegment


class AlignedSegment(ExtractionSegment):
    start: float
    end: float
    speaker_ids: list[str] = Field(default_factory=list)
    ambiguous: bool = False
