"""Per-operation local JSON logs. Never retain/log prompts, text or model output."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
from time import perf_counter

logger = logging.getLogger("app.metrics")


def approximate_tokens(text: str) -> int:
    """Rough UTF-8 bytes/4 estimate; NOT a tokenizer or a context-limit check."""
    return (len(text.encode("utf-8")) + 3) // 4


@dataclass
class AnalysisMetrics:
    operation: str
    meeting_id: str | None = None
    model: str | None = None
    transcript_characters: int | None = None
    input_tokens_estimate: int = 0
    cache_status: str = "miss"
    llm_calls: int = 0

    def sources(self, segments) -> None:
        self.transcript_characters = sum(len(segment.text) for segment in segments)

    def llm_request(self, *, system: str, user: str, schema: dict) -> None:
        # Count logical generate_json attempts, including failed/context-rejected ones.
        # /api/show, cache lookups, GETs and exports are not LLM generation calls.
        self.llm_calls += 1
        self.input_tokens_estimate += approximate_tokens(system + user + json.dumps(schema, ensure_ascii=False))

    def cached(self, analysis, *, original_model: str | None = None) -> None:
        self.cache_status = "hit"
        self.sources(analysis.source_segments)
        self.model = analysis.model or original_model or None


_current: ContextVar[AnalysisMetrics | None] = ContextVar("analysis_metrics", default=None)


@contextmanager
def analysis_operation(operation: str, *, meeting_id: str | None = None,
                       model: str | None = None, reuse_current: bool = False):
    """Analyzer joins its enclosing API operation; standalone calls emit once too."""
    existing = _current.get()
    if reuse_current and existing is not None:
        yield existing
        return
    metrics = AnalysisMetrics(operation=operation, meeting_id=meeting_id, model=model)
    token = _current.set(metrics)
    started = perf_counter()
    status = "success"
    try:
        yield metrics
    except BaseException:
        status = "error"
        raise
    finally:
        elapsed = max(0.0, perf_counter() - started)
        _current.reset(token)
        payload = {"event": "analysis_metrics", **vars(metrics), "duration_seconds": round(elapsed, 6),
                   "status": status, "input_tokens_estimate_method": "utf8_bytes_div_4",
                   "input_tokens_estimate_scope": "system_user_json_schema_excluding_server_template"}
        logger.info("analysis_metrics %s", json.dumps(payload, ensure_ascii=True), extra={"analysis_metrics": payload})
