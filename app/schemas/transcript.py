from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TimedText(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("end must not precede start")
        return self


class WordTimestamp(TimedText):
    probability: float | None = Field(default=None, ge=0, le=1)


class TranscriptSegment(TimedText):
    id: int
    words: list[WordTimestamp] | None = None


class TranscriptRaw(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    schema_version: int = 1
    engine: Literal["faster-whisper"] = "faster-whisper"
    task: Literal["transcribe"] = "transcribe"
    model: str
    device: str
    compute_type: str
    text: str
    detected_language: str | None = None
    language_probability: float | None = Field(default=None, ge=0, le=1)
    language_scope: Literal["initial_detection"] = "initial_detection"
    duration_seconds: float = Field(ge=0)
    segments: list[TranscriptSegment]
    options: dict
    warnings: list[str] = Field(default_factory=list)
