import json

import pytest

from app.core.config import Settings
from app.schemas.action_item import ExtractionSegment
from app.services.analysis import AnalysisError, LocalMeetingAnalyzer
from app.services.local_llm import LocalLLMError


def empty_draft():
    return {"action_items": [], "summary": {"topics": [], "key_discussions": [], "decisions": [], "problems_and_risks": [], "main_action_item_indices": []}}


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply, ensure_ascii=False)


def overflow():
    return LocalLLMError("llm_context_exceeded", "context", 413)


def test_short_meeting_exactly_one_joint_call():
    llm = FakeLLM([empty_draft()])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=4, text="Обсудили результаты.")])
    assert len(llm.calls) == result.generation_requests == result.completed_generations == 1
    assert llm.calls[0]["schema"]["title"] == "AnalysisDraft"
    assert result.mode == "single" and result.chunks == 0


def test_chunks_are_facts_then_one_merge_with_late_deadline_change():
    segments = [ExtractionSegment(id=0, text="Нурлан, проведите аудит. Две недели хватит?"),
                ExtractionSegment(id=1, text="Нет. Хорошо, три недели.")]
    chunks = [{"facts": [{"kind": kind, "segment_id": s.id, "quote": s.text}], "action_items": []}
              for kind, s in zip(["assignment", "deadline_change"], segments)]
    merged = empty_draft()
    merged["action_items"] = [dict(description="Провести аудит", responsible="Нурлан", deadline_raw="три недели",
        source_segment_ids=[0, 1], confidence=0.8, decision_type="assigned", condition=None, milestones=[],
        evidence=[dict(segment_id=s.id, quote=s.text) for s in segments])]
    merged["summary"]["main_action_item_indices"] = [0]
    llm = FakeLLM([overflow(), *chunks, merged])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze(segments)
    assert result.completed_generations == 3 and result.generation_requests == 4
    assert [call["schema"]["title"] for call in llm.calls] == ["AnalysisDraft", "ChunkFacts", "ChunkFacts", "AnalysisDraft"]
    assert "chunks" in json.loads(llm.calls[-1]["user"])
    assert result.summary.main_action_items[0].deadline_raw == "три недели"
    assert result.summary.main_action_items[0].source_segment_ids == [0, 1]


def test_invalid_response_and_server_error_do_not_trigger_more_inference():
    for response in ({"invalid": True}, LocalLLMError("llm_unavailable", "offline")):
        llm = FakeLLM([response])
        with pytest.raises(LocalLLMError):
            LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=0, text="Текст")])
        assert len(llm.calls) == 1


def test_budget_stops_without_final_merge():
    llm = FakeLLM([overflow(), {"facts": [], "action_items": []}])
    with pytest.raises(AnalysisError) as error:
        LocalMeetingAnalyzer(llm, Settings(_env_file=None, analysis_max_requests=2)).analyze(
            [ExtractionSegment(id=i, text="Текст") for i in range(2)])
    assert error.value.code == "analysis_budget_exceeded" and len(llm.calls) == 2


def test_empty_input_zero_calls():
    llm = FakeLLM([])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([])
    assert result.mode == "empty" and not llm.calls


def test_single_large_segment_splits_without_changing_id_or_losing_text():
    original = ExtractionSegment(id=17, text="Очень длинная реплика с несколькими существенными фактами.")
    llm = FakeLLM([overflow(), {"facts": [], "action_items": []}, {"facts": [], "action_items": []}, empty_draft()])
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([original])
    fragments = [json.loads(c["user"])["segments"][0] for c in llm.calls[1:3]]
    assert "".join(s["text"] for s in fragments) == original.text
    assert [s["id"] for s in fragments] == [17, 17]
    assert result.source_segments == [original]


def test_merge_overflow_does_not_start_recursive_summaries():
    llm = FakeLLM([overflow(), {"facts": [], "action_items": []}, {"facts": [], "action_items": []}, overflow()])
    with pytest.raises(LocalLLMError) as error:
        LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(id=i, text="Текст") for i in range(2)])
    assert error.value.code == "llm_context_exceeded"
    assert len(llm.calls) == 4
