from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HACKALEM_", env_file=ROOT / ".env",
        env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    app_name: str = "HackAlem AI"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = ROOT / "data"
    database_path: Path | None = None
    stt_model: str = Field(default="large-v3", validation_alias=AliasChoices("WHISPER_MODEL", "HACKALEM_STT_MODEL"))
    stt_device: Literal["cpu", "cuda", "auto"] = Field(default="auto", validation_alias=AliasChoices("WHISPER_DEVICE", "HACKALEM_STT_DEVICE"))
    stt_compute_type: str = Field(default="auto", validation_alias=AliasChoices("WHISPER_COMPUTE_TYPE", "HACKALEM_STT_COMPUTE_TYPE"))
    whisper_model_path: Path | None = Field(default=None, validation_alias="WHISPER_MODEL_PATH")
    whisper_language: str | None = Field(default=None, validation_alias="WHISPER_LANGUAGE")
    whisper_multilingual: bool = Field(default=True, validation_alias="WHISPER_MULTILINGUAL")
    whisper_word_timestamps: bool = Field(default=True, validation_alias="WHISPER_WORD_TIMESTAMPS")
    whisper_vad_filter: bool = Field(default=True, validation_alias="WHISPER_VAD_FILTER")
    whisper_beam_size: int = Field(default=5, ge=1, le=20, validation_alias="WHISPER_BEAM_SIZE")
    whisper_chunk_length: int = Field(default=30, ge=1, le=30, validation_alias="WHISPER_CHUNK_LENGTH")
    whisper_cpu_threads: int = Field(default=4, ge=1, validation_alias="WHISPER_CPU_THREADS")
    whisper_condition_on_previous_text: bool = Field(default=False, validation_alias="WHISPER_CONDITION_ON_PREVIOUS_TEXT")
    diarization_model_path: Path = Field(
        default=ROOT / "models/speaker-diarization-community-1",
        validation_alias=AliasChoices("DIARIZATION_MODEL_PATH", "HACKALEM_DIARIZATION_MODEL_PATH"),
    )
    diarization_device: Literal["cpu", "cuda", "auto"] = Field(default="auto", validation_alias="DIARIZATION_DEVICE")
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    llm_timeout_seconds: float = Field(default=180, gt=0, le=1800)
    llm_num_ctx: int = Field(default=32768, ge=4096, le=262144)
    llm_num_predict: int = Field(default=4096, ge=256, le=32768)
    extraction_max_chars: int = Field(default=60000, ge=1)
    extraction_attempts: int = Field(default=2, ge=1, le=3)
    summary_max_chars: int = Field(default=60000, ge=1)
    summary_attempts: int = Field(default=2, ge=1, le=3)
    ffmpeg_path: str = "ffmpeg"
    max_upload_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    max_audio_seconds: int = Field(default=4 * 60 * 60, gt=0)
    ffmpeg_timeout_seconds: int = Field(default=600, gt=0)

    @field_validator("data_dir", "database_path", "diarization_model_path", "whisper_model_path")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.resolve() if value.is_absolute() else (ROOT / value).resolve()

    @field_validator("ollama_base_url")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username is not None or url.password is not None
            or url.query or url.fragment or url.path not in {"", "/"}
        ):
            raise ValueError("Ollama must use a local HTTP loopback endpoint")
        _ = url.port  # Reject malformed ports during configuration validation.
        return value.rstrip("/")

    @property
    def sqlite_path(self) -> Path:
        return self.database_path or self.data_dir / "meetings.sqlite3"

    @field_validator("whisper_language", mode="before")
    @classmethod
    def auto_language(cls, value):
        return None if value is None or str(value).strip().lower() in {"", "auto"} else str(value).strip().lower()

    @model_validator(mode="after")
    def check_whisper(self):
        if self.llm_num_predict >= self.llm_num_ctx:
            raise ValueError("LLM_NUM_PREDICT must be less than LLM_NUM_CTX")
        if self.whisper_multilingual and self.whisper_language is not None:
            raise ValueError("WHISPER_LANGUAGE must be auto with WHISPER_MULTILINGUAL=true")
        if not self.stt_model.strip() or self.stt_model.endswith(".en"):
            raise ValueError("Use a multilingual Whisper model, not an English-only model")
        return self

    @property
    def local_whisper_path(self) -> Path:
        # Model ID is metadata, never an arbitrary path component.
        import re
        name = re.sub(r"[^A-Za-z0-9_-]", "_", self.stt_model)
        return self.whisper_model_path or ROOT / "models" / "whisper" / name
