from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SpeakerId = Annotated[str, StringConstraints(pattern=r"^SPEAKER_[0-9]{2,}$")]
SpeakerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class DiarizationSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    speaker_id: SpeakerId
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class SpeakerMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: dict[SpeakerId, SpeakerName] = Field(default_factory=dict, max_length=1000)


class SpeakerRead(BaseModel):
    speaker_id: SpeakerId
    name: str | None = None
