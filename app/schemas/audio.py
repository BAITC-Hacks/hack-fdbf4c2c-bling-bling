from typing import Literal
from uuid import UUID
from pydantic import BaseModel


class AudioRead(BaseModel):
    id: UUID
    status: Literal["audio_ready"] = "audio_ready"
    format: Literal["wav"] = "wav"
    channels: Literal[1] = 1
    sample_rate: Literal[16000] = 16000
    duration_seconds: float
