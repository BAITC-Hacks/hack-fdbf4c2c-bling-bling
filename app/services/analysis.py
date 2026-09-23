"""One joint inference; map compact facts then one merge only after context rejection."""
import json

from app.core.config import Settings
from app.prompts import load_prompt
from app.schemas.action_item import ActionItemExtractionResult, ExtractionSegment
from app.schemas.analysis import AnalysisDraft, ChunkFacts, MeetingAnalysis
from app.schemas.summary import MeetingSummary
from app.services.extraction import deduplicate, normalized, strict_json, validate_grounding
from app.services.local_llm import LocalLLMClient, LocalLLMError
from app.services.summarization import validate_summary
from app.services.analysis_metrics import AnalysisMetrics, analysis_operation


class AnalysisError(LocalLLMError):
    pass


class LocalMeetingAnalyzer:
    def __init__(self, llm: LocalLLMClient, settings: Settings):
        self.llm, self.settings = llm, settings

    def analyze(self, segments: list[ExtractionSegment]) -> MeetingAnalysis:
        with analysis_operation("analyze", model=self.settings.ollama_model, reuse_current=True) as metrics:
            metrics.model = self.settings.ollama_model or None
            metrics.sources(segments)
            result = self._analyze(segments, metrics)
            result.model = metrics.model
            return result

    def _analyze(self, segments: list[ExtractionSegment], metrics: AnalysisMetrics) -> MeetingAnalysis:
        if len({s.id for s in segments}) != len(segments):
            raise AnalysisError("analysis_invalid_sources", "ID сегментов должны быть уникальны.", 422)
        if not segments or not any(s.text.strip() for s in segments):
            return MeetingAnalysis(action_items=[], summary=MeetingSummary(topics=[], key_discussions=[], decisions=[], problems_and_risks=[], main_action_items=[]),
                                   source_segments=segments, mode="empty", generation_requests=0, completed_generations=0, chunks=0)
        requests = completed = 0
        partials = []

        def call(schema, prompt, payload):
            nonlocal requests, completed
            if requests >= self.settings.analysis_max_requests:
                raise AnalysisError("analysis_budget_exceeded", "Достигнут лимит LLM-запросов; увеличьте контекст модели.", 413)
            requests += 1
            arguments = dict(system=prompt, user=json.dumps(payload, ensure_ascii=False), schema=schema.model_json_schema())
            metrics.llm_request(**arguments)
            raw = self.llm.generate_json(**arguments)
            completed += 1
            try:
                return schema.model_validate(strict_json(raw))
            except ValueError:
                raise AnalysisError("analysis_invalid_output", "Невалидный JSON анализа. Автоматический повтор inference отключён.", 502) from None

        def payload(part):
            return {"segments": [s.model_dump() for s in part]}

        def split(part):
            if len(part) > 1:
                # Balance text volume, rather than assuming equal-length segments.
                total = sum(len(s.text) for s in part)
                accumulated = 0
                middle = 1
                for i, segment in enumerate(part[:-1], 1):
                    accumulated += len(segment.text)
                    middle = i
                    if accumulated >= total / 2:
                        break
                return part[:middle], part[middle:]
            source = part[0]
            if len(source.text) < 2:
                raise AnalysisError("analysis_context_too_small", "Даже минимальный фрагмент не помещается в контекст вместе с инструкцией.", 413)
            middle = len(source.text) // 2
            boundary = source.text.rfind(" ", 0, middle + 1)
            if boundary > len(source.text) // 4:
                middle = boundary + 1
            # Fragments keep the original global ID; evidence validates against
            # the fragment here and against the full original segment at merge.
            return ([source.model_copy(update={"text": source.text[:middle]})],
                    [source.model_copy(update={"text": source.text[middle:]})])

        def collect(part):
            if len(partials) >= self.settings.analysis_max_chunks:
                raise AnalysisError("analysis_chunk_limit", "Достигнут лимит частей; увеличьте контекст модели.", 413)
            try:
                facts = call(ChunkFacts, load_prompt("chunk_facts.txt"), payload(part))
            except LocalLLMError as error:
                if error.code != "llm_context_exceeded":
                    raise
                left, right = split(part)
                collect(left)
                collect(right)
                return
            try:
                validate_grounding(ActionItemExtractionResult(action_items=facts.action_items), part)
                sources = {s.id: s.text for s in part}
                for fact in facts.facts:
                    if fact.segment_id not in sources or normalized(fact.quote) not in normalized(sources[fact.segment_id]):
                        raise ValueError()
            except ValueError:
                raise AnalysisError("analysis_invalid_chunk", "Факты части не подтверждены исходными репликами.", 502) from None
            # Keep confirmed speaker names beside facts for cross-part attribution.
            partials.append({**facts.model_dump(), "speakers": {str(s.id): s.speaker for s in part if s.speaker}})

        prompt = load_prompt("meeting_analyzer.txt")
        try:
            draft = call(AnalysisDraft, prompt, payload(segments))
            mode = "single"
        except LocalLLMError as error:
            if error.code != "llm_context_exceeded":
                raise
            # Only a real context overflow enables chunking. No byte heuristic here.
            left, right = split(segments)
            collect(left)
            collect(right)
            # Exactly one final merge. If it does not fit, fail without tree summaries.
            draft = call(AnalysisDraft, prompt, {"chunks": partials})
            mode = "chunked"
        try:
            actions = ActionItemExtractionResult(action_items=draft.action_items)
            validate_grounding(actions, segments)
            validate_summary(draft.summary, segments, len(actions.action_items))
            selected = [actions.action_items[i].model_copy(deep=True) for i in draft.summary.main_action_item_indices]
            actions = deduplicate(actions)
            selected = deduplicate(ActionItemExtractionResult(action_items=selected)).action_items
            # Link selections back to the fully merged action, preserving all evidence.
            def key(item):
                return (normalized(item.description).rstrip(".!"), normalized(item.responsible or ""), item.decision_type, normalized(item.condition or ""))
            merged = {key(item): item for item in actions.action_items}
            summary = MeetingSummary(**draft.summary.model_dump(exclude={"main_action_item_indices"}), main_action_items=[merged[key(item)].model_copy(deep=True) for item in selected])
        except ValueError:
            raise AnalysisError("analysis_validation_failed", "Анализ не прошёл проверку ссылок, цитат, сроков или чисел.", 502) from None
        return MeetingAnalysis(action_items=actions.action_items, summary=summary, source_segments=segments, mode=mode,
                               generation_requests=requests, completed_generations=completed, chunks=len(partials))
