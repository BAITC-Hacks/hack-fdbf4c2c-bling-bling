from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
SegmentId = Annotated[int, Field(ge=0, strict=True)]
SourceIds = Annotated[list[SegmentId], Field(min_length=1, max_length=1000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ExtractionSegment(StrictModel):
    id: SegmentId
    text: str = Field(min_length=1)
    # Caller-supplied confirmed metadata only; the raw STT route leaves this null.
    speaker: Text | None = None


class Evidence(StrictModel):
    segment_id: SegmentId
    quote: Text


class Milestone(StrictModel):
    description: Text
    deadline_raw: Text | None
    source_segment_ids: SourceIds


class ActionItem(StrictModel):
    description: Text
    responsible: Text | None
    deadline_raw: Text | None
    source_segment_ids: SourceIds
    confidence: float = Field(ge=0, le=1, description="Uncalibrated model self-assessment, not a probability guarantee")
    decision_type: Literal["assigned", "conditional"]
    condition: Text | None
    milestones: list[Milestone] = Field(max_length=100)
    evidence: list[Evidence] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def consistent_condition(self):
        if (self.decision_type == "conditional") != (self.condition is not None):
            raise ValueError("Conditional decisions require a condition; assigned tasks must have null condition")
        return self


class ActionItemExtractionResult(StrictModel):
    action_items: list[ActionItem] = Field(max_length=300)


class ActionItemExtractionRead(ActionItemExtractionResult):
    source_segments: list[ExtractionSegment]
    source_transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str
    prompt_version: Literal["1", "2", "joint-1"] = "2"
