"""Provider-independent contract. Meeting analysis imports only this module."""
from typing import Protocol


class LocalLLMError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class LocalLLMClient(Protocol):
    def generate_json(self, *, system: str, user: str, schema: dict) -> str:
        """Return JSON text from a local model, or raise LocalLLMError."""
        ...
