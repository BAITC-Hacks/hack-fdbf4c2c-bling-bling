import pytest

from app.prompts import load_prompt
from app.services.local_llm import LocalLLMError


def test_utf8_prompt_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompt = load_prompt("action_items.txt")
    assert "транскрипт корпоративного совещания" in prompt
    assert "Запрещено придумывать людей" in prompt
    assert "казахском" in prompt
    assert "???" not in prompt


@pytest.mark.parametrize("content", [None, "  ", b"\xff"])
def test_missing_empty_or_corrupt_prompt_fails_explicitly(tmp_path, monkeypatch, content):
    monkeypatch.setattr("app.prompts.PROMPTS_DIR", tmp_path)
    if content is not None:
        (tmp_path / "action_items.txt").write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(LocalLLMError) as error:
        load_prompt("action_items.txt")
    assert error.value.code == "prompt_unavailable"
