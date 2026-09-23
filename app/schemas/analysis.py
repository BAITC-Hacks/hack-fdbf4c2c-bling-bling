from typing import Literal

from pydantic import Field

from app.schemas.action_item import ActionItem, Evidence, ExtractionSegment, StrictModel
from app.schemas.summary import MeetingSummary, SummaryDraft


class AnalysisDraft(StrictModel):
    action_items: list[ActionItem] = Field(max_length=300)
    summary: SummaryDraft


class ChunkFact(Evidence):
    kind: Literal["fact", "problem", "proposal", "decision", "condition", "assignment", "deadline_change", "response"]


class ChunkFacts(StrictModel):
    facts: list[ChunkFact] = Field(max_length=200)
    action_items: list[ActionItem] = Field(max_length=100)


class MeetingAnalysis(StrictModel):
    # Optional for compatibility with results saved before metrics were introduced.
    model: str | None = None
    action_items: list[ActionItem]
    summary: MeetingSummary
    source_segments: list[ExtractionSegment]
    mode: Literal["single", "chunked", "empty", "reused"]
    generation_requests: int
    completed_generations: int
    chunks: int
