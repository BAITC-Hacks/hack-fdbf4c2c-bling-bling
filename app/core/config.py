from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HACKALEM_", env_file=ROOT / ".env",
        env_file_encoding="utf-8", extra="ignore",
    )

    app_name: str = "HackAlem AI"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = ROOT / "data"
    database_path: Path | None = None
    stt_model: str = "large-v3"
    stt_device: Literal["cpu", "cuda", "auto"] = "cpu"
    stt_compute_type: str = "int8"
    diarization_model_path: Path = ROOT / "models/speaker-diarization-community-1"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""

    @field_validator("data_dir", "database_path", "diarization_model_path")
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
