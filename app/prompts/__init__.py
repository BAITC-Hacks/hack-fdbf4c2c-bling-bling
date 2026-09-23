"""UTF-8 prompt assets resolved independently of the working directory."""
from pathlib import Path

from app.services.local_llm import LocalLLMError

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    if name not in {"action_items.txt", "meeting_summary.txt", "meeting_analyzer.txt", "chunk_facts.txt"}:
        raise ValueError("Unknown prompt asset")
    try:
        prompt = (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError("Empty prompt")
        return prompt
    except (OSError, ValueError):
        raise LocalLLMError("prompt_unavailable", "Файл инструкции локальной модели отсутствует, пуст или повреждён.") from None
