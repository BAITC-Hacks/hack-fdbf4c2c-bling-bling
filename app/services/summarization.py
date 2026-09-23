"""Evidence-linked summary via the same provider-independent local LLM contract."""
import json
import re

from app.core.config import Settings
from app.prompts import load_prompt
from app.schemas.action_item import ActionItemExtractionResult, ExtractionSegment
from app.schemas.summary import MeetingSummary, SummaryDraft
from app.services.extraction import normalized, strict_json, validate_grounding
from app.services.local_llm import LocalLLMClient


class SummaryError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def numeric_literals(text: str) -> set[str]:
    # Lexical safeguard, not a semantic validator: e.g. 94% differs from 94.
    return {re.sub(r"\s", "", value) for value in re.findall(r"[+-]?\d+(?:[.,]\d+)*(?:\s*%)?", text)}


def validate_summary(draft: SummaryDraft, segments: list[ExtractionSegment], action_count: int) -> None:
    sources = {s.id: s for s in segments}
    for point in [*draft.topics, *draft.key_discussions, *draft.decisions, *draft.problems_and_risks]:
        ids = set(point.source_segment_ids)
        if not ids <= sources.keys() or ids != {e.segment_id for e in point.evidence}:
            raise ValueError("invalid_sources")
        for evidence in point.evidence:
            if normalized(evidence.quote) not in normalized(sources[evidence.segment_id].text):
                raise ValueError("ungrounded_quote")
        quoted_numbers = set().union(*(numeric_literals(e.quote) for e in point.evidence))
        if not numeric_literals(point.text) <= quoted_numbers:
            raise ValueError("changed_numbers")
    for decision in draft.decisions:
        if decision.condition is not None and not any(normalized(decision.condition) in normalized(e.quote) for e in decision.evidence):
            raise ValueError("ungrounded_condition")
    indices = draft.main_action_item_indices
    if len(indices) != len(set(indices)) or any(i >= action_count for i in indices) or (action_count and not indices):
        raise ValueError("invalid_action_selection")


class MeetingSummaryService:
    def __init__(self, llm: LocalLLMClient, settings: Settings):
        self.llm = llm
        self.settings = settings

    def summarize(self, segments: list[ExtractionSegment], action_items: ActionItemExtractionResult) -> MeetingSummary:
        if len({s.id for s in segments}) != len(segments):
            raise SummaryError("summary_duplicate_segment_ids", "ID исходных сегментов должны быть уникальными.")
        try:
            validate_grounding(action_items, segments)
        except ValueError:
            raise SummaryError("summary_invalid_actions", "Поручения не соответствуют исходным сегментам.") from None
        if not segments or not any(s.text.strip() for s in segments):
            return MeetingSummary(topics=[], key_discussions=[], decisions=[], problems_and_risks=[], main_action_items=[])
        if sum(len(s.text) for s in segments) > self.settings.summary_max_chars:
            raise SummaryError("summary_input_too_large", "Транскрипт превышает лимит summary; текст не обрезается.", 413)
        schema = SummaryDraft.model_json_schema()
        system = load_prompt("meeting_summary.txt") + "\nJSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
        payload = json.dumps({"segments": [s.model_dump() for s in segments],
                              "action_items": [{"index": i, **item.model_dump()} for i, item in enumerate(action_items.action_items)]}, ensure_ascii=False)
        for attempt in range(self.settings.summary_attempts):
            correction = "" if attempt == 0 else "\nПредыдущий ответ не прошёл проверку. Перепроверь схему, цитаты, ID, числа, условия и индексы поручений."
            raw = self.llm.generate_json(system=system + correction, user=payload, schema=schema)
            try:
                draft = SummaryDraft.model_validate(strict_json(raw))
                validate_summary(draft, segments, len(action_items.action_items))
                return MeetingSummary(**draft.model_dump(exclude={"main_action_item_indices"}),
                                      main_action_items=[action_items.action_items[i].model_copy(deep=True) for i in draft.main_action_item_indices])
            except ValueError:
                continue
        raise SummaryError("summary_invalid_output", "LLM не вернула корректное summary с подтверждёнными цитатами, числами и ссылками. Результат не сохранён.", 502)
