"""Action extraction independent of the model server and persistence layer."""
import json
import re
import unicodedata

from pydantic import ValidationError

from app.core.config import Settings
from app.prompts import load_prompt
from app.schemas.action_item import ActionItemExtractionResult, ExtractionSegment
from app.services.local_llm import LocalLLMClient



class ExtractionError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def validate_grounding(result: ActionItemExtractionResult, segments: list[ExtractionSegment]) -> None:
    sources = {segment.id: segment for segment in segments}
    for item in result.action_items:
        ids = set(item.source_segment_ids)
        if not ids <= sources.keys() or ids != {e.segment_id for e in item.evidence}:
            raise ValueError("invalid_source_ids")
        for evidence in item.evidence:
            if normalized(evidence.quote) not in normalized(sources[evidence.segment_id].text):
                raise ValueError("ungrounded_quote")
        texts = [normalized(sources[key].text) for key in ids]
        speakers = [normalized(sources[key].speaker or "") for key in ids]
        if item.responsible is not None and not any(normalized(item.responsible) in text for text in texts + speakers):
            raise ValueError("ungrounded_responsible")
        for value in (item.deadline_raw, item.condition):
            if value is not None and not any(normalized(value) in text for text in texts):
                raise ValueError("ungrounded_deadline_or_condition")
        for milestone in item.milestones:
            if not set(milestone.source_segment_ids) <= ids:
                raise ValueError("invalid_milestone_sources")
            if milestone.deadline_raw is not None and not any(
                normalized(milestone.deadline_raw) in normalized(sources[key].text)
                for key in milestone.source_segment_ids
            ):
                raise ValueError("ungrounded_milestone_deadline")


def deduplicate(result: ActionItemExtractionResult) -> ActionItemExtractionResult:
    """Merge exact semantic-field duplicates only; never guess between conflicts."""
    unique = {}
    for item in result.action_items:
        key = (normalized(item.description).rstrip(".!"), normalized(item.responsible or ""),
               item.decision_type, normalized(item.condition or ""))
        item.source_segment_ids = sorted(set(item.source_segment_ids))
        for milestone in item.milestones:
            milestone.source_segment_ids = sorted(set(milestone.source_segment_ids))
        previous = unique.get(key)
        if previous is None:
            unique[key] = item
            continue
        milestone_key = lambda m: (normalized(m.description), normalized(m.deadline_raw or ""))
        if normalized(previous.deadline_raw or "") != normalized(item.deadline_raw or "") or sorted(map(milestone_key, previous.milestones)) != sorted(map(milestone_key, item.milestones)):
            raise ValueError("duplicate_action_conflict")
        previous.source_segment_ids = sorted(set(previous.source_segment_ids + item.source_segment_ids))
        evidence_keys = {(e.segment_id, e.quote) for e in previous.evidence}
        previous.evidence.extend(e for e in item.evidence if (e.segment_id, e.quote) not in evidence_keys)
        for milestone in previous.milestones:
            matching = [m for m in item.milestones if milestone_key(m) == milestone_key(milestone)]
            milestone.source_segment_ids = sorted(set(milestone.source_segment_ids + [i for m in matching for i in m.source_segment_ids]))
        previous.confidence = min(previous.confidence, item.confidence)
    return ActionItemExtractionResult(action_items=list(unique.values()))


def strict_json(text: str) -> dict:
    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj
    def invalid_constant(value):
        raise ValueError("non_finite_json_number")
    return json.loads(text, object_pairs_hook=unique_keys, parse_constant=invalid_constant)


class ActionItemExtractionService:
    def __init__(self, llm: LocalLLMClient, settings: Settings):
        self.llm = llm
        self.settings = settings

    def extract(self, segments: list[ExtractionSegment]) -> ActionItemExtractionResult:
        if len({s.id for s in segments}) != len(segments):
            raise ExtractionError("duplicate_segment_ids", "ID исходных сегментов должны быть уникальными.")
        if not segments or not any(s.text.strip() for s in segments):
            return ActionItemExtractionResult(action_items=[])
        if sum(len(s.text) for s in segments) > self.settings.extraction_max_chars:
            raise ExtractionError("extraction_input_too_large", "Транскрипт превышает лимит извлечения; текст не обрезается.", 413)
        schema = ActionItemExtractionResult.model_json_schema()
        payload = json.dumps({"segments": [s.model_dump() for s in segments]}, ensure_ascii=False)
        system = load_prompt("action_items.txt") + "\nJSON Schema:\n" + json.dumps(schema, ensure_ascii=False)
        for attempt in range(self.settings.extraction_attempts):
            correction = "" if attempt == 0 else "\nПредыдущий ответ не прошел проверку. Заново проверь JSON, реальные ID и цитаты, сроки, условия и дубликаты."
            raw = self.llm.generate_json(system=system + correction, user=payload, schema=schema)
            try:
                result = ActionItemExtractionResult.model_validate(strict_json(raw))
                validate_grounding(result, segments)
                result = deduplicate(result)
                validate_grounding(result, segments)
                return result
            except (ValueError, ValidationError):
                # No unvalidated model output or transcript in logs/errors.
                continue
        raise ExtractionError("extraction_invalid_output", "LLM не вернула корректные поручения с подтвержденными ссылками и цитатами. Результат не сохранён.", 502)
