import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.main import create_app
from app.models.meeting import Meeting
from app.schemas.transcript import TranscriptRaw, TranscriptSegment
from app.services.extraction import ActionItemExtractionService
from app.services.summarization import MeetingSummaryService

TEXT = "Ерлан, до пятницы подготовьте претензию. Выпуск — 94% плана."


class StubLLM:
    """Explicit contract double shared by extraction and summary."""
    def generate_json(self, **kwargs):
        if kwargs["schema"]["title"] == "ActionItemExtractionResult":
            result = {"action_items": [dict(description="Подготовить претензию", responsible="Ерлан", deadline_raw="до пятницы",
                       source_segment_ids=[0], confidence=0.9, decision_type="assigned", condition=None, milestones=[],
                       evidence=[dict(segment_id=0, quote=TEXT)])]}
        else:
            result = dict(topics=[dict(text="Производственные показатели", source_segment_ids=[0], evidence=[dict(segment_id=0, quote=TEXT)])],
                          key_discussions=[], decisions=[], problems_and_risks=[], main_action_item_indices=[0])
        return json.dumps(result, ensure_ascii=False)


@pytest.fixture
def prepared(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_path=tmp_path / "db.sqlite3", ollama_model="")
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Summary contract"}).json()["id"]
        transcript = TranscriptRaw(model="fixture", device="cpu", compute_type="int8", text=TEXT, duration_seconds=3, options={},
                                   segments=[TranscriptSegment(id=0, start=0, end=3, text=TEXT)])
        target = tmp_path / "processed" / meeting_id / "transcript_raw.json"
        target.parent.mkdir()
        target.write_text(transcript.model_dump_json(), encoding="utf-8")
        with client.app.state.session_factory() as session:
            session.get(Meeting, meeting_id).status = "transcribed"
            session.commit()
        client.app.state.extraction_service = ActionItemExtractionService(StubLLM(), settings)
        yield client, meeting_id, settings, target


def test_extract_summarize_persist_restart(prepared):
    client, meeting_id, settings, _ = prepared
    prefix = f"/meetings/{meeting_id}"
    actions = client.post(prefix + "/extract-action-items").json()
    client.app.state.summary_service = MeetingSummaryService(StubLLM(), settings)
    response = client.post(prefix + "/summarize")
    assert response.status_code == 200, response.text
    assert response.json()["main_action_items"] == actions["action_items"]
    assert response.json()["source_segments"] == actions["source_segments"]
    assert client.get(prefix + "/summary/status").json()["status"] == "ready"
    assert client.get(prefix + "/transcript").status_code == 200
    assert client.get(prefix + "/action-items").status_code == 200
    assert client.get(prefix + "/status").json()["status"] == "transcribed"
    assert client.post(prefix + "/summarize").status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(prefix + "/summary").json() == response.json()


def test_preconditions_and_source_revision(prepared):
    client, meeting_id, _, target = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.post(f"/meetings/{uuid4()}/summarize").status_code == 404
    assert client.get(prefix + "/summary").status_code == 409
    assert client.post(prefix + "/summarize").json()["detail"]["code"] == "action_items_not_ready"
    assert client.post(prefix + "/extract-action-items").status_code == 200
    transcript = TranscriptRaw.model_validate_json(target.read_text(encoding="utf-8"))
    transcript.segments[0].text = "Изменённый транскрипт"
    target.write_text(transcript.model_dump_json(), encoding="utf-8")
    assert client.post(prefix + "/summarize").json()["detail"]["code"] == "summary_source_mismatch"
    assert client.get(prefix + "/summary/status").json()["status"] == "not_started"


def test_unavailable_model_invalid_output_and_retry(prepared):
    client, meeting_id, settings, _ = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.post(prefix + "/extract-action-items").status_code == 200
    assert client.post(prefix + "/summarize").json()["detail"]["code"] == "llm_not_configured"
    assert client.get(prefix + "/summary/status").json()["status"] == "not_started"
    class BadLLM:
        def generate_json(self, **kwargs):
            return '{"topics": "fiction"}'
    client.app.state.summary_service = MeetingSummaryService(BadLLM(), settings)
    assert client.post(prefix + "/summarize").status_code == 502
    assert client.get(prefix + "/summary").status_code == 409
    client.app.state.summary_service = MeetingSummaryService(StubLLM(), settings)
    assert client.post(prefix + "/summarize").status_code == 200


def test_concurrent_summary_claim(prepared):
    client, meeting_id, settings, _ = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.post(prefix + "/extract-action-items").status_code == 200
    class CheckingLLM(StubLLM):
        def generate_json(self, **kwargs):
            assert client.get(prefix + "/summary/status").json()["status"] == "running"
            assert client.post(prefix + "/summarize").status_code == 409
            return super().generate_json(**kwargs)
    client.app.state.summary_service = MeetingSummaryService(CheckingLLM(), settings)
    assert client.post(prefix + "/summarize").status_code == 200
