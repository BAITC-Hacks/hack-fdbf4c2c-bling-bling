"""Validation/transport contracts, not a model quality benchmark."""
import copy
import json

import pytest

from app.core.config import Settings
from app.schemas.action_item import ExtractionSegment
from app.services.extraction import ActionItemExtractionService, ExtractionError


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.replies)


@pytest.fixture
def example():
    segments = [ExtractionSegment(id=15, text="Ерлан, до пятницы подготовьте претензию.", speaker="Руководитель"),
                ExtractionSegment(id=16, text="Повторю: Ерлан, до пятницы подготовьте претензию.", speaker="Руководитель")]
    item = dict(description="Подготовить претензию", responsible="Ерлан", deadline_raw="до пятницы",
                source_segment_ids=[15], confidence=0.92, decision_type="assigned", condition=None,
                milestones=[], evidence=[dict(segment_id=15, quote=segments[0].text)])
    return segments, item


def extract(segments, items, **settings):
    llm = ScriptedLLM([json.dumps({"action_items": items}, ensure_ascii=False)])
    service = ActionItemExtractionService(llm, Settings(_env_file=None, extraction_attempts=1, **settings))
    return service.extract(segments), llm


def test_schema_and_sources_without_assigning_speaker(example):
    segments, item = example
    result, llm = extract(segments, [item])
    assert result.action_items[0].responsible == "Ерлан"
    assert result.action_items[0].source_segment_ids == [15]
    assert json.loads(llm.calls[0]["user"])["segments"][0]["id"] == 15
    assert llm.calls[0]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("field,value", [
    ("source_segment_ids", [999]), ("responsible", "Выдуманное имя"),
    ("deadline_raw", "2030-01-01"), ("confidence", 1.1), ("confidence", "0.9"),
    ("decision_type", "proposal"), ("condition", "Если будет дождь"),
    ("evidence", [{"segment_id": 15, "quote": "претензию уже подготовили"}]),
    ("source_segment_ids", [True]),
    ("milestones", [{"description": "Черновик", "deadline_raw": "завтра", "source_segment_ids": [15]}]),
    ("milestones", [{"description": "Черновик", "deadline_raw": None, "source_segment_ids": [16]}]),
])
def test_reject_ungrounded_or_invalid_fields(example, field, value):
    segments, item = example
    item[field] = value
    with pytest.raises(ExtractionError) as error:
        extract(segments, [item])
    assert error.value.code == "extraction_invalid_output"
    assert "Ерлан" not in str(error.value)


def test_duplicate_merges_all_evidence_and_confidence(example):
    segments, item = example
    repeated = copy.deepcopy(item)
    repeated.update(source_segment_ids=[16], evidence=[dict(segment_id=16, quote=segments[1].text)], confidence=0.8)
    result, _ = extract(segments, [item, repeated])
    assert len(result.action_items) == 1
    assert result.action_items[0].source_segment_ids == [15, 16]
    assert len(result.action_items[0].evidence) == 2
    assert result.action_items[0].confidence == 0.8


def test_conflicting_duplicates_retry_instead_of_guessing(example):
    segments, item = example
    segments.append(ExtractionSegment(id=17, text="Ерлан, срок изменён: до понедельника."))
    revised = copy.deepcopy(item)
    revised.update(deadline_raw="до понедельника", source_segment_ids=[17], evidence=[dict(segment_id=17, quote=segments[-1].text)])
    llm = ScriptedLLM([json.dumps({"action_items": [item, revised]}), json.dumps({"action_items": [revised]})])
    result = ActionItemExtractionService(llm, Settings(_env_file=None)).extract(segments)
    assert len(llm.calls) == 2
    assert len(result.action_items) == 1
    assert result.action_items[0].deadline_raw == "до понедельника"


@pytest.mark.parametrize("raw", ['```json\n{"action_items": []}\n```', '{"action_items": [], "extra": 1}',
                                  '{"action_items": [], "action_items": []}', '{"action_items": NaN}', '{}'])
def test_strict_json_no_best_effort_parsing(example, raw):
    segments, _ = example
    service = ActionItemExtractionService(ScriptedLLM([raw]), Settings(_env_file=None, extraction_attempts=1))
    with pytest.raises(ExtractionError):
        service.extract(segments)


def test_empty_input_and_limits_do_not_call_model(example):
    segments, _ = example
    llm = ScriptedLLM([])
    service = ActionItemExtractionService(llm, Settings(_env_file=None, extraction_max_chars=1))
    assert service.extract([]).action_items == []
    with pytest.raises(ExtractionError) as error:
        service.extract(segments)
    assert error.value.status == 413
    with pytest.raises(ExtractionError) as error:
        service.extract([segments[0], segments[0]])
    assert error.value.code == "duplicate_segment_ids"
    assert llm.calls == []


def test_conditional_decision_and_multiple_milestones():
    segments = [ExtractionSegment(id=0, text="Если подрядчик продолжит нарушать — расторгнем договор."),
                ExtractionSegment(id=1, text="Ерболат: смета за неделю, закрыть очередь за месяц.")]
    items = [dict(description="Расторгнуть договор", responsible=None, deadline_raw=None, source_segment_ids=[0],
                  confidence=0.8, decision_type="conditional", condition="Если подрядчик продолжит нарушать",
                  milestones=[], evidence=[dict(segment_id=0, quote=segments[0].text)]),
             dict(description="Организовать переаттестацию", responsible="Ерболат", deadline_raw=None, source_segment_ids=[1],
                  confidence=0.9, decision_type="assigned", condition=None,
                  milestones=[dict(description="Смета", deadline_raw="за неделю", source_segment_ids=[1]),
                              dict(description="Закрыть очередь", deadline_raw="за месяц", source_segment_ids=[1])],
                  evidence=[dict(segment_id=1, quote=segments[1].text)])]
    result, _ = extract(segments, items)
    assert result.action_items[0].condition is not None
    assert [m.deadline_raw for m in result.action_items[1].milestones] == ["за неделю", "за месяц"]


@pytest.mark.parametrize("text,name,deadline", [
    ("Марат, жұмаға дейін есеп дайындаңыз.", "Марат", "жұмаға дейін"),
    ("Гульмира, есепті дайындаңыз до пятницы.", "Гульмира", "до пятницы"),
    ("Юридический департамент, проверьте контракт.", "Юридический департамент", None),
    ("Подготовьте проект договора.", None, None),
])
def test_original_language_department_and_nulls_preserved(text, name, deadline):
    segments = [ExtractionSegment(id=0, text=text)]
    item = dict(description="Задание из реплики", responsible=name, deadline_raw=deadline, source_segment_ids=[0],
                confidence=0.8, decision_type="assigned", condition=None, milestones=[], evidence=[dict(segment_id=0, quote=text)])
    result, _ = extract(segments, [item])
    assert result.action_items[0].responsible == name
    assert result.action_items[0].deadline_raw == deadline
