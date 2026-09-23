"""Contracts on the two supplied protocols; scripted replies are NOT LLM quality tests."""
import copy
import json
from pathlib import Path
import re
import socket

import pytest

from app.core.config import Settings
from app.schemas.action_item import ExtractionSegment
from app.services.analysis import AnalysisError, LocalMeetingAnalyzer

ROOT = Path(__file__).resolve().parents[2]
DATA = json.loads((ROOT / "tests/fixtures/meeting_protocols.json").read_text(encoding="utf-8"))
PROTOCOLS = {protocol["id"]: protocol for protocol in DATA["protocols"]}


class MockLocalLLMClient:
    def __init__(self, response):
        self.response = copy.deepcopy(response)
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.response, ensure_ascii=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unit tests must not contact a model or network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def analyze(number, response=None, ids=None):
    protocol = PROTOCOLS[f"protocol_{number}"]
    segments = [ExtractionSegment.model_validate(s) for s in protocol["segments"] if ids is None or s["id"] in ids]
    llm = MockLocalLLMClient(protocol["mock_response"] if response is None else response)
    result = LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze(segments)
    assert len(llm.calls) == result.generation_requests == 1
    assert json.loads(llm.calls[0]["user"]) == {"segments": [s.model_dump() for s in segments]}
    assert llm.calls[0]["schema"]["title"] == "AnalysisDraft"
    return result, segments


@pytest.mark.parametrize("number,count", [(1, 32), (2, 28)])
def test_fixtures_cover_actual_speech_without_supplied_summary(number, count):
    protocol = PROTOCOLS[f"protocol_{number}"]
    source = (ROOT / DATA["source"]).read_text(encoding="utf-8")
    speech = source.split(protocol["source_heading"], 1)[1].split("# Саммари по ключевым пунктам", 1)[0]
    assert len(protocol["segments"]) == count
    assert [s["id"] for s in protocol["segments"]] == list(range(count))
    assert re.findall(r"^\*\*([^*:\n]+)\*\*[^\n]*$", speech, flags=re.M) == [s["speaker"] for s in protocol["segments"]]
    positions = [speech.index(s["text"]) for s in protocol["segments"]]
    assert positions == sorted(positions)


@pytest.mark.parametrize("number,count", [(1, 11), (2, 6)])
def test_full_protocol_reference_survives_joint_analysis(number, count):
    result, segments = analyze(number)
    assert len(result.action_items) == count
    assert result.summary.main_action_items == result.action_items
    assert result.source_segments == segments


def test_responsible_is_not_the_chair_speaking():
    result, segments = analyze(2)
    claim = result.action_items[0]
    assert segments[4].speaker == "Данияр Серикович"
    assert claim.responsible == "Ерлан" != segments[4].speaker
    assert claim.deadline_raw == "до конца недели"
    assert 27 in claim.source_segment_ids  # Recap must not reassign to Ботагоз.


def test_assignee_who_never_speaks_is_retained():
    result, segments = analyze(2)
    assert "Ерлан" not in {segment.speaker for segment in segments}
    assert result.action_items[0].responsible == "Ерлан"


def test_department_is_valid_assignee():
    result, _ = analyze(1)
    item = result.action_items[3]
    assert item.responsible == "юридический департамент"
    assert item.deadline_raw == "до 30 сентября"


def test_revised_deadline_keeps_negotiation_and_final_sources():
    result, _ = analyze(1)
    audit = result.action_items[7]
    assert audit.deadline_raw == "к пятнадцатому октября"
    assert {17, 18, 19, 31} <= set(audit.source_segment_ids)
    quotes = " ".join(e.quote for e in audit.evidence)
    assert "Две недели" in quotes and "три недели" in quotes
    assert result.summary.main_action_items[7].deadline_raw == audit.deadline_raw


def test_missing_deadline_not_filled_from_invoice_contract_rule():
    result, _ = analyze(2)
    item = result.action_items[5]
    assert item.responsible == "Салтанат Ерболовна"
    assert "пять рабочих дней" in " ".join(e.quote for e in item.evidence)
    assert item.deadline_raw is None and item.milestones == []


def test_relative_deadlines_and_multiple_milestones_not_converted_to_dates():
    result, _ = analyze(2)
    assert result.action_items[1].deadline_raw == "за две недели"
    assert result.action_items[3].deadline_raw == "по итогам этого совещания"
    assert [m.deadline_raw for m in result.action_items[4].milestones] == ["за неделю", "за месяц"]
    assert result.summary.main_action_items[4].milestones == result.action_items[4].milestones


def test_recap_duplicates_merge_sources_in_actions_and_summary():
    response = copy.deepcopy(PROTOCOLS["protocol_1"]["mock_response"])
    instruction = response["action_items"][8]
    original = copy.deepcopy(instruction)
    original["source_segment_ids"] = [25, 27, 28]
    original["evidence"] = [e for e in original["evidence"] if e["segment_id"] != 31]
    recap = copy.deepcopy(instruction)
    recap["source_segment_ids"] = [31]
    recap["evidence"] = [e for e in recap["evidence"] if e["segment_id"] == 31]
    response["action_items"][8] = original
    response["action_items"].append(recap)
    response["summary"]["main_action_item_indices"].append(11)
    result, _ = analyze(1, response)
    assert len(result.action_items) == len(result.summary.main_action_items) == 11
    assert result.action_items[8].source_segment_ids == [25, 27, 28, 31]
    assert {e.segment_id for e in result.action_items[8].evidence} == {25, 27, 28, 31}


def test_conditional_termination_remains_conditional_in_both_views():
    result, _ = analyze(1)
    task, decision = result.action_items[6], result.summary.decisions[0]
    assert task.decision_type == decision.decision_type == "conditional"
    assert task.condition == decision.condition == "Если систематически нарушают"
    assert task.deadline_raw is None


def test_proposal_alone_vs_later_explicit_assignment():
    proposal_only = dict(action_items=[], summary=dict(topics=[], key_discussions=[], decisions=[],
                        problems_and_risks=[], main_action_item_indices=[]))
    result, _ = analyze(1, proposal_only, ids=[21])
    assert result.action_items == result.summary.decisions == []
    # This is a second independent input snapshot, not automatic extra inference.
    accepted = copy.deepcopy(PROTOCOLS["protocol_1"]["mock_response"]["action_items"][9])
    accepted["source_segment_ids"] = [21, 22, 23]
    accepted["evidence"] = [e for e in accepted["evidence"] if e["segment_id"] != 31]
    proposal_only["action_items"] = [accepted]
    proposal_only["summary"]["main_action_item_indices"] = [0]
    result, _ = analyze(1, proposal_only, ids=[21, 22, 23])
    assert result.action_items[0].responsible == "Тимур Болатович"
    assert result.action_items[0].deadline_raw == "на этой неделе"


@pytest.mark.parametrize("field,value", [("responsible", "Несуществующий исполнитель"),
                                         ("deadline_raw", "2099-10-15"), ("source_segment_ids", [999])])
def test_corrupted_reference_rejected_without_retry(field, value):
    protocol = PROTOCOLS["protocol_2"]
    response = copy.deepcopy(protocol["mock_response"])
    response["action_items"][0][field] = value
    llm = MockLocalLLMClient(response)
    with pytest.raises(AnalysisError):
        LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(**s) for s in protocol["segments"]])
    assert len(llm.calls) == 1


def test_conflicting_old_and_new_deadlines_not_guessed_or_retried():
    protocol = PROTOCOLS["protocol_1"]
    response = copy.deepcopy(protocol["mock_response"])
    old = copy.deepcopy(response["action_items"][7])
    old["deadline_raw"] = "Две недели"
    response["action_items"].append(old)
    llm = MockLocalLLMClient(response)
    with pytest.raises(AnalysisError, match="Анализ не прошёл"):
        LocalMeetingAnalyzer(llm, Settings(_env_file=None)).analyze([ExtractionSegment(**s) for s in protocol["segments"]])
    assert len(llm.calls) == 1
