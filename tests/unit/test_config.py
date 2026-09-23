import pytest
from pydantic import ValidationError
from app.core.config import Settings


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / "settings.env"
    dotenv.write_text("HACKALEM_APP_NAME=File name\nHACKALEM_LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.setenv("HACKALEM_APP_NAME", "Environment name")
    config = Settings(_env_file=dotenv)
    assert config.app_name == "Environment name"
    assert config.log_level == "WARNING"


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.2:11434",
    "http://localhost.evil.test", "http://user:secret@localhost:11434", "http://localhost/proxy"])
def test_reject_nonlocal_llm(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_base_url=url)


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"])
def test_loopback_llm(url):
    assert Settings(_env_file=None, ollama_base_url=url).ollama_base_url == url
