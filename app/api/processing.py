"""Resumable orchestration using existing STT/diarization stages and one analyzer."""
import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.meetings import require_meeting
from app.api.transcripts import transcribe, get_transcript
from app.api.diarization import diarize, read_result
from app.db.database import get_session
from app.models.action_item import MeetingExtraction
from app.models.audio import MeetingAudio
from app.models.diarization import MeetingDiarization
from app.models.pipeline import MeetingAnalysisRecord, MeetingPipeline
from app.models.summary import MeetingSummaryRecord
from app.prompts import load_prompt
from app.schemas.action_item import ActionItemExtractionRead, ActionItemExtractionResult, ExtractionSegment
from app.schemas.analysis import MeetingAnalysis
from app.schemas.result import MeetingResult
from app.schemas.meeting import MeetingRead
from app.schemas.summary import MeetingSummary, MeetingSummaryRead, SummaryDraft
from app.services.export import ExportError
from app.services.extraction import validate_grounding
from app.services.local_llm import LocalLLMError
from app.services.summarization import validate_summary
from app.services.analysis_metrics import analysis_operation

router = APIRouter(prefix="/meetings", tags=["pipeline"])
DatabaseSession = Annotated[Session, Depends(get_session)]


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pipeline_state(record: MeetingPipeline):
    return {"id": record.meeting_id, "status": record.status, "stage": record.stage, "error_code": record.error_code,
            "exports": [f"/meetings/{record.meeting_id}/exports/{kind}" for kind in (record.exports or {})]}


def reuse_analysis(extraction, summary, sources, transcript_hash):
    """Reuse compatible historical results without spending another inference."""
    actions = ActionItemExtractionRead.model_validate(extraction.result)
    saved_summary = MeetingSummaryRead.model_validate(summary.result)
    action_result = ActionItemExtractionResult(action_items=actions.action_items)
    if actions.source_transcript_sha256 != transcript_hash or saved_summary.source_transcript_sha256 != transcript_hash or saved_summary.source_action_items_sha256 != sha(action_result.model_dump_json()):
        raise HTTPException(409, detail={"code": "analysis_inputs_changed", "message": "Сохранённый анализ относится к другой версии источников."})
    view = MeetingSummary(**{key: getattr(saved_summary, key) for key in MeetingSummary.model_fields})
    indices = [actions.action_items.index(item) for item in view.main_action_items]
    validate_grounding(action_result, sources)
    validate_summary(SummaryDraft(**view.model_dump(exclude={"main_action_items"}), main_action_item_indices=indices), sources, len(actions.action_items))
    return MeetingAnalysis(model=actions.model, action_items=actions.action_items, summary=view, source_segments=sources,
                           mode="reused", generation_requests=0, completed_generations=0, chunks=0)


@router.post("/{id}/analyze")
@router.post("/{id}/process")
def process(id: UUID, request: Request, session: DatabaseSession):
    with analysis_operation("analyze", meeting_id=str(id), model=request.app.state.settings.ollama_model or None) as metrics:
        return run_pipeline(id, request, session, metrics)


def note_cached_analysis(metrics, analysis, session, id):
    original = session.get(MeetingExtraction, str(id)) if not analysis.model else None
    model = original.result.get("model") if original and original.result else None
    metrics.cached(analysis, original_model=model)


def run_pipeline(id, request, session, metrics):
    meeting = require_meeting(session, id)
    if session.get(MeetingAudio, str(id)) is None:
        raise HTTPException(409, detail={"code": "audio_not_ready", "message": "Сначала загрузите запись через /upload."})
    record = session.get(MeetingPipeline, str(id))
    if record and record.status == "ready":
        cached = session.get(MeetingAnalysisRecord, str(id))
        metrics.cache_status = "hit"
        metrics.model = None
        if cached:
            note_cached_analysis(metrics, MeetingAnalysis.model_validate(cached.result), session, id)
        return pipeline_state(record)
    if record is None:
        record = MeetingPipeline(meeting_id=str(id), status="running", stage="transcription")
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, detail={"code": "pipeline_busy", "message": "Pipeline уже запущен."}) from None
    else:
        claimed = session.execute(update(MeetingPipeline).where(MeetingPipeline.meeting_id == str(id), MeetingPipeline.status == "failed")
                                  .values(status="running", error_code=None))
        session.commit()
        if not claimed.rowcount:
            raise HTTPException(409, detail={"code": "pipeline_busy", "message": "Pipeline уже запущен."})
    reserved = []
    completed = False

    def stage(name):
        record.stage = name
        session.commit()

    try:
        stage("transcription")
        transcript = get_transcript(id, request, session) if meeting.status == "transcribed" else transcribe(
            id, request, session, request.app.state.transcription_service)
        stage("diarization")
        diarization = session.get(MeetingDiarization, str(id))
        turns = read_result(request, id) if diarization and diarization.status == "ready" else diarize(
            id, request, session, request.app.state.diarization_service)
        diarization = session.get(MeetingDiarization, str(id))
        stage("alignment")
        alignment = request.app.state.alignment_service.align(transcript, turns, diarization.speaker_names)
        sources = [ExtractionSegment(id=s.id, text=s.text, speaker=s.speaker) for s in alignment]
        metrics.sources(sources)
        transcript_hash = sha(transcript.model_dump_json())
        fingerprint = sha(json.dumps({"transcript": transcript_hash, "alignment": [s.model_dump() for s in alignment],
            "model": request.app.state.settings.ollama_model,
            "prompts": [load_prompt("meeting_analyzer.txt"), load_prompt("chunk_facts.txt")]}, ensure_ascii=False, sort_keys=True))
        stage("analysis")
        cached = session.get(MeetingAnalysisRecord, str(id))
        if cached:
            note_cached_analysis(metrics, MeetingAnalysis.model_validate(cached.result), session, id)
            if cached.fingerprint != fingerprint:
                raise HTTPException(409, detail={"code": "analysis_inputs_changed", "message": "Источники или модель изменились. Создайте новую встречу для нового анализа."})
            analysis = MeetingAnalysis.model_validate(cached.result)
        else:
            existing = [session.get(model, str(id)) for model in (MeetingExtraction, MeetingSummaryRecord)]
            if any(row and row.status != "ready" for row in existing):
                raise HTTPException(409, detail={"code": "analysis_busy", "message": "Другая операция анализа ещё не завершена."})
            if all(existing):
                analysis = reuse_analysis(*existing, sources, transcript_hash)
                note_cached_analysis(metrics, analysis, session, id)
            else:
                # Reserve compatibility views, restoring earlier results if analysis fails.
                for model, old in zip((MeetingExtraction, MeetingSummaryRecord), existing):
                    row = old or model(meeting_id=str(id), status="running")
                    reserved.append((row, old.result if old else None))
                    row.status = "running"
                    session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    reserved.clear()
                    raise HTTPException(409, detail={"code": "analysis_busy", "message": "Анализ запущен другим запросом."}) from None
                analysis = request.app.state.meeting_analyzer.analyze(sources)
                stage("validation")
                actions = ActionItemExtractionResult(action_items=analysis.action_items)
                action_view = ActionItemExtractionRead(action_items=analysis.action_items, source_segments=sources,
                    source_transcript_sha256=transcript_hash, model=request.app.state.settings.ollama_model, prompt_version="joint-1")
                summary_view = MeetingSummaryRead(**analysis.summary.model_dump(), source_segments=sources,
                    source_transcript_sha256=transcript_hash, source_action_items_sha256=sha(actions.model_dump_json()),
                    model=request.app.state.settings.ollama_model, prompt_version="joint-1")
                stage("database")
                reserved[0][0].result, reserved[0][0].status = action_view.model_dump(mode="json"), "ready"
                reserved[1][0].result, reserved[1][0].status = summary_view.model_dump(mode="json"), "ready"
            session.add(MeetingAnalysisRecord(meeting_id=str(id), fingerprint=fingerprint,
                result=analysis.model_dump(mode="json"), alignment=[s.model_dump() for s in alignment]))
            session.commit()
            reserved.clear()
        stage("export")
        record.exports = request.app.state.export_service.export(str(id), meeting.title, analysis, alignment, meeting.meeting_date)
        record.status, record.stage, record.error_code = "ready", "complete", None
        session.commit()
        completed = True
        return pipeline_state(record)
    except HTTPException as error:
        record.error_code = error.detail.get("code", "pipeline_failed") if isinstance(error.detail, dict) else "pipeline_failed"
        raise
    except (LocalLLMError, ExportError) as error:
        record.error_code = error.code
        raise HTTPException(error.status, detail={"code": error.code, "message": error.message}) from None
    except Exception:
        record.error_code = "pipeline_failed"
        raise HTTPException(500, detail={"code": "pipeline_failed", "message": "Ошибка локального pipeline; готовые стадии сохранены."}) from None
    finally:
        if not completed:
            code = record.error_code or "pipeline_failed"
            session.rollback()
            for row, old_result in reserved:
                # An inserted uncommitted row may have been rolled back entirely.
                persisted = session.get(type(row), str(id))
                if persisted is not None and persisted.status == "running":
                    if old_result is None:
                        session.delete(persisted)
                    else:
                        persisted.status, persisted.result = "ready", old_result
            record.status, record.error_code = "failed", code
            session.commit()


@router.get("/{id}/pipeline/status")
def status(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingPipeline, str(id))
    return pipeline_state(record) if record else {"id": str(id), "status": "not_started", "stage": None, "error_code": None, "exports": []}


@router.get("/{id}/analysis", response_model=MeetingAnalysis)
def get_analysis(id: UUID, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingAnalysisRecord, str(id))
    if record is None:
        raise HTTPException(409, detail={"code": "analysis_not_ready", "message": "Единый анализ ещё не готов."})
    return MeetingAnalysis.model_validate(record.result)


@router.get("/{id}/result", response_model=MeetingResult)
def get_result(id: UUID, session: DatabaseSession):
    """Read the committed analysis snapshot only; never invoke processing/services."""
    with analysis_operation("result_read", meeting_id=str(id)) as metrics:
        meeting = require_meeting(session, id)
        record = session.get(MeetingAnalysisRecord, str(id))
        if record is None:
            raise HTTPException(409, detail={"code": "analysis_not_ready", "message": "Результат анализа ещё не готов."})
        analysis = MeetingAnalysis.model_validate(record.result)
        note_cached_analysis(metrics, analysis, session, id)
        return MeetingResult(meeting=MeetingRead.model_validate(meeting), summary=analysis.summary,
                             action_items=analysis.action_items, transcript=record.alignment)


@router.get("/{id}/exports/{format}")
def get_export(id: UUID, format: Literal["docx", "pdf"], request: Request, session: DatabaseSession):
    require_meeting(session, id)
    record = session.get(MeetingPipeline, str(id))
    path = request.app.state.settings.data_dir / "exports" / str(id) / f"protocol.{format}"
    if record is None or record.status != "ready" or not path.is_file():
        raise HTTPException(409, detail={"code": "export_not_ready", "message": "Экспорт ещё не готов."})
    media = "application/pdf" if format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(path, media_type=media, filename=f"protocol-{id}.{format}")
