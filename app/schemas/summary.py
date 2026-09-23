from typing import Literal

from pydantic import Field, model_validator

from app.schemas.action_item import ActionItem, Evidence, ExtractionSegment, SegmentId, SourceIds, StrictModel, Text


class SummaryPoint(StrictModel):
    text: Text
    source_segment_ids: SourceIds
    evidence: list[Evidence] = Field(min_length=1, max_length=1000)


class SummaryDecision(SummaryPoint):
    decision_type: Literal["accepted", "conditional"]
    condition: Text | None

    @model_validator(mode="after")
    def consistent_condition(self):
        if (self.decision_type == "conditional") != (self.condition is not None):
            raise ValueError("Only conditional decisions require a condition")
        return self


class SummarySections(StrictModel):
    topics: list[SummaryPoint] = Field(max_length=50)
    key_discussions: list[SummaryPoint] = Field(max_length=100)
    decisions: list[SummaryDecision] = Field(max_length=100)
    problems_and_risks: list[SummaryPoint] = Field(max_length=100)


class SummaryDraft(SummarySections):
    main_action_item_indices: list[SegmentId] = Field(max_length=300)


class MeetingSummary(SummarySections):
    main_action_items: list[ActionItem] = Field(max_length=300)


class MeetingSummaryRead(MeetingSummary):
    source_segments: list[ExtractionSegment]
    source_transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_action_items_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str
    prompt_version: Literal["1"] = "1"
