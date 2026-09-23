import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.config import Settings
from app.schemas.action_item import ExtractionSegment
from app.services.analysis import LocalMeetingAnalyzer
from app.services.analysis_metrics import analysis_operation
from app.services.local_llm import LocalLLMError


def draft():
    return dict(action_items=[], summary=dict(topics=[], key_discussions=[], decisions=[],
                problems_and_risks=[], main_action_item_indices=[]))


class ScriptedClient:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply, ensure_ascii=False)


def events(caplog):
    return [record.analysis_metrics for record in caplog.records if hasattr(record, "analysis_metrics")]


@pytest.fixture(autouse=True)
def capture_metrics(caplog):
    caplog.set_level(logging.INFO, logger="app.metrics")


def test_one_call_counts_prompt_schema_input_and_duration_without_logging_text(caplog, monkeypatch):
    times = iter([10.0, 12.25])
    monkeypatch.setattr("app.services.analysis_metrics.perf_counter", lambda: next(times))
    text = "Секретный протокол Әә: 123456"
    llm = ScriptedClient([draft()])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None, ollama_model="local-fixture")).analyze([ExtractionSegment(id=0, text=text)])
    event, = events(caplog)
    assert event["transcript_characters"] == len(text)
    assert event["model"] == result.model == "local-fixture"
    assert event["duration_seconds"] == 2.25
    assert event["cache_status"] == "miss" and event["status"] == "success"
    assert event["llm_calls"] == result.generation_requests == len(llm.calls) == 1
    arguments = llm.calls[0]
    rendered = arguments["system"] + arguments["user"] + json.dumps(arguments["schema"], ensure_ascii=False)
    assert event["input_tokens_estimate"] == (len(rendered.encode("utf-8")) + 3) // 4
    assert event["input_tokens_estimate_method"] == "utf8_bytes_div_4"
    message = next(record.getMessage() for record in caplog.records if hasattr(record, "analysis_metrics"))
    assert json.loads(message.removeprefix("analysis_metrics ")) == event
    assert text not in message and "123456" not in message


@pytest.mark.parametrize("reply", [LocalLLMError("llm_unavailable", "private server message"), {"bad": "private output"}])
def test_failures_still_report_one_attempt_without_leaking_details(caplog, reply):
    llm = ScriptedClient([reply])
    with pytest.raises(LocalLLMError):
        LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=0, text="private transcript")])
    event, = events(caplog)
    assert event["status"] == "error" and event["llm_calls"] == 1
    assert event["input_tokens_estimate"] > 0
    assert "private" not in caplog.text


def test_empty_input_reports_zero_llm_calls(caplog):
    LocalMeetingAnalyzer(ScriptedClient([]), Settings(_env_file=None)).analyze([])
    event, = events(caplog)
    assert event["transcript_characters"] == event["input_tokens_estimate"] == event["llm_calls"] == 0


def test_chunks_include_rejected_request_and_final_merge(caplog):
    llm = ScriptedClient([LocalLLMError("llm_context_exceeded", "overflow", 413),
                          {"facts": [], "action_items": []}, {"facts": [], "action_items": []}, draft()])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=i, text="Текст") for i in range(2)])
    event, = events(caplog)
    assert event["transcript_characters"] == 10  # Original input, not repeated chunk payloads.
    assert event["llm_calls"] == result.generation_requests == 4
    assert result.completed_generations == 3
    assert event["input_tokens_estimate"] > 0


def test_api_scope_joins_analyzer_and_emits_once(caplog):
    llm = ScriptedClient([draft()])
    with analysis_operation("analyze", meeting_id="example-id"):
        LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=0, text="Текст")])
    event, = events(caplog)
    assert event["meeting_id"] == "example-id" and event["llm_calls"] == 1


def test_concurrent_operations_do_not_share_counters(caplog):
    barrier = Barrier(2)
    def run(model):
        class Client:
            def generate_json(self, **kwargs):
                barrier.wait(timeout=5)
                return json.dumps(draft())
        return LocalMeetingAnalyzer(Client(), Settings(_env_file=None, ollama_model=model)).analyze([ExtractionSegment(id=0, text=model)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert [r.generation_requests for r in pool.map(run, ["one", "second"])] == [1, 1]
    recorded = {event["model"]: event for event in events(caplog)}
    assert recorded["one"]["transcript_characters"] == 3
    assert recorded["second"]["transcript_characters"] == 6
    assert all(event["llm_calls"] == 1 for event in recorded.values())


def test_failed_operation_does_not_pollute_the_next(caplog):
    with pytest.raises(RuntimeError):
        with analysis_operation("analyze", meeting_id="first"):
            raise RuntimeError("secret")
    LocalMeetingAnalyzer(ScriptedClient([draft()]), Settings(_env_file=None)).analyze([ExtractionSegment(id=0, text="Текст")])
    first, second = events(caplog)
    assert first["status"] == "error" and first["llm_calls"] == 0
    assert second["meeting_id"] is None and second["llm_calls"] == 1
