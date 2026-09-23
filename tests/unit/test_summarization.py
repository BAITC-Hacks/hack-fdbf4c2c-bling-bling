import copy
import json

import pytest

from app.core.config import Settings
from app.schemas.action_item import ActionItemExtractionResult, ExtractionSegment
from app.services.summarization import MeetingSummaryService, SummaryError


class ScriptedLLM:
    """Canned responses: these tests verify contracts, not LLM accuracy."""
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(next(self.replies), ensure_ascii=False)


@pytest.fixture
def example():
    segments = [ExtractionSegment(id=10, text="Выпуск — 94% плана. Пострадавших нет."),
                ExtractionSegment(id=11, text="Может быть стоит поменять поставщика. Решение не принято."),
                ExtractionSegment(id=12, text="Ерлан, до пятницы подготовьте претензию."),
                ExtractionSegment(id=13, text="Если подрядчик продолжит нарушать — расторгнем договор.")]
    point = lambda i, text: dict(text=text, source_segment_ids=[i], evidence=[dict(segment_id=i, quote=next(s.text for s in segments if s.id == i))])
    draft = dict(topics=[point(10, "Производственные показатели")],
                 key_discussions=[point(10, "Выпуск составляет 94% плана. Пострадавших нет."), point(11, "Обсуждалось предложение поменять поставщика, решение не принято.")],
                 decisions=[dict(**point(13, "Условное расторжение договора при продолжении нарушений."), decision_type="conditional", condition="Если подрядчик продолжит нарушать")],
                 problems_and_risks=[], main_action_item_indices=[0])
    actions = ActionItemExtractionResult.model_validate({"action_items": [dict(
        description="Подготовить претензию", responsible="Ерлан", deadline_raw="до пятницы",
        source_segment_ids=[12], confidence=0.9, decision_type="assigned", condition=None, milestones=[],
        evidence=[dict(segment_id=12, quote=segments[2].text)])]})
    return segments, actions, draft


def service(*drafts, **settings):
    llm = ScriptedLLM(drafts)
    return MeetingSummaryService(llm, Settings(_env_file=None, summary_attempts=len(drafts) or 1, **settings)), llm


def test_all_sections_and_exact_action_copy(example):
    segments, actions, draft = example
    processor, llm = service(draft)
    result = processor.summarize(segments, actions)
    assert set(result.model_dump()) == {"topics", "key_discussions", "decisions", "problems_and_risks", "main_action_items"}
    assert result.main_action_items[0] == actions.action_items[0]
    assert result.main_action_items[0] is not actions.action_items[0]
    assert result.decisions[0].condition == "Если подрядчик продолжит нарушать"
    assert llm.calls[0]["schema"]["additionalProperties"] is False
    payload = json.loads(llm.calls[0]["user"])
    assert payload["action_items"][0]["index"] == 0
    assert "Ты анализируешь транскрипт корпоративного совещания" in llm.calls[0]["system"]


@pytest.mark.parametrize("text", ["Выпуск составляет 95% плана.", "Выпуск составляет 94 единицы.", "Уволены 12 человек.", "Срок — 2030 год."])
def test_changed_or_invented_numbers_rejected(example, text):
    segments, actions, draft = example
    draft["key_discussions"][0]["text"] = text
    processor, _ = service(draft)
    with pytest.raises(SummaryError) as error:
        processor.summarize(segments, actions)
    assert error.value.code == "summary_invalid_output"


@pytest.mark.parametrize("indices", [[99], [True], [0, 0], [-1], []])
def test_unknown_or_duplicate_action_indices(example, indices):
    segments, actions, draft = example
    draft["main_action_item_indices"] = indices
    with pytest.raises(SummaryError):
        service(draft)[0].summarize(segments, actions)


@pytest.mark.parametrize("change", ["id", "quote", "condition", "proposal", "extra_deadline"])
def test_invalid_evidence_decisions_or_new_fields(example, change):
    segments, actions, draft = example
    if change == "id":
        draft["topics"][0]["source_segment_ids"] = [999]
    elif change == "quote":
        draft["topics"][0]["evidence"][0]["quote"] = "Увеличить выпуск на 94%."
    elif change == "condition":
        draft["decisions"][0]["condition"] = None
    elif change == "proposal":
        draft["decisions"][0]["decision_type"] = "proposal"
    else:
        draft["decisions"][0]["deadline_raw"] = "завтра"
    with pytest.raises(SummaryError):
        service(draft)[0].summarize(segments, actions)


def test_retry_and_multiple_action_deadlines_preserved(example):
    segments, actions, draft = example
    segments[2].text += " Смета за неделю, закрыть очередь за месяц."
    # The added milestone deadlines must also be in the evidence shown to the user.
    actions.action_items[0].evidence[0].quote = segments[2].text
    from app.schemas.action_item import Milestone
    actions.action_items[0].milestones = [Milestone(description="Смета", deadline_raw="за неделю", source_segment_ids=[12]),
                                         Milestone(description="Закрыть очередь", deadline_raw="за месяц", source_segment_ids=[12])]
    invalid = copy.deepcopy(draft)
    invalid["topics"][0]["text"] = "Выпуск 99%"
    processor, llm = service(invalid, draft)
    result = processor.summarize(segments, actions)
    assert len(llm.calls) == 2
    assert result.main_action_items[0].milestones == actions.action_items[0].milestones


def test_no_llm_for_empty_invalid_or_oversized_input(example):
    segments, actions, _ = example
    empty = ActionItemExtractionResult(action_items=[])
    processor, llm = service(summary_max_chars=1)
    assert processor.summarize([], empty).main_action_items == []
    with pytest.raises(SummaryError) as error:
        processor.summarize(segments, actions)
    assert error.value.status == 413
    with pytest.raises(SummaryError):
        processor.summarize([segments[0], segments[0]], empty)
    with pytest.raises(SummaryError) as error:
        processor.summarize([], actions)
    assert error.value.code == "summary_invalid_actions"
    assert llm.calls == []


@pytest.mark.parametrize("text", ["Жүктеме 71%.", "Жүктеме 71%, подготовка отчёта обсуждалась."])
def test_kazakh_and_mixed_unicode_preserved(text):
    draft = dict(topics=[], key_discussions=[dict(text=text, source_segment_ids=[0], evidence=[dict(segment_id=0, quote=text)])],
                 decisions=[], problems_and_risks=[], main_action_item_indices=[])
    result = service(draft)[0].summarize([ExtractionSegment(id=0, text=text)], ActionItemExtractionResult(action_items=[]))
    assert result.key_discussions[0].text == text
