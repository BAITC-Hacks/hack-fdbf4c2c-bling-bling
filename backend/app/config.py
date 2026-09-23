from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'sqlite:///./data/test.db'
    service_token: str
    webhook_token: str
    app_secret: str
    admin_email: str = 'admin@local.test'
    admin_password: str
    ollama_url: str = 'http://ollama:11434'
    n8n_url: str = 'http://n8n:5678'
    llm_model: str = 'qwen3:4b-instruct-2507-q4_K_M'
    embedding_model: str = 'qwen3-embedding:0.6b'
    qdrant_url: str = 'http://qdrant:6333'
    qdrant_api_key: str = ''
    data_dir: Path = Path('/data')
    asr_model: str = '/models/whisper-small'
    speaker_model: str = '/models/speaker.onnx'
    lease_seconds: int = 900


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
