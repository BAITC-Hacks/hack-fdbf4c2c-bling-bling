import json
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.main import create_app
from app.models.meeting import Meeting
from app.schemas.transcript import TranscriptRaw, TranscriptSegment
from app.services.extraction import ActionItemExtractionService


class StubLLM:
    """Canned model response for contract tests; no claimed semantic inference."""
    def generate_json(self, **kwargs):
        return json.dumps({"action_items": [{
            "description": "Подготовить претензию", "responsible": "Ерлан", "deadline_raw": "до пятницы",
            "source_segment_ids": [15], "confidence": 0.9, "decision_type": "assigned", "condition": None,
            "milestones": [], "evidence": [{"segment_id": 15, "quote": "Ерлан, до пятницы подготовьте претензию."}],
        }]}, ensure_ascii=False)


@pytest.fixture
def prepared(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_path=tmp_path / "test.sqlite3", ollama_model="")
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Test"}).json()["id"]
        transcript = TranscriptRaw(model="contract-fixture", device="cpu", compute_type="int8", text="Ерлан, до пятницы подготовьте претензию.",
                                   duration_seconds=3, options={}, segments=[TranscriptSegment(id=15, start=0, end=3, text="Ерлан, до пятницы подготовьте претензию.")])
        directory = tmp_path / "processed" / meeting_id
        directory.mkdir()
        (directory / "transcript_raw.json").write_text(transcript.model_dump_json(), encoding="utf-8")
        with client.app.state.session_factory() as session:
            session.get(Meeting, meeting_id).status = "transcribed"
            session.commit()
        yield client, meeting_id, settings


def use_stub(client, settings):
    client.app.state.extraction_service = ActionItemExtractionService(StubLLM(), settings)


def test_persists_sources_restart_and_keeps_transcript(prepared):
    client, meeting_id, settings = prepared
    prefix = f"/meetings/{meeting_id}"
    use_stub(client, settings)
    assert client.get(prefix + "/action-items/status").json()["status"] == "not_started"
    response = client.post(prefix + "/extract-action-items")
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["action_items"][0]["responsible"] == "Ерлан"
    assert saved["source_segments"][0]["id"] == 15
    assert len(saved["source_transcript_sha256"]) == 64
    assert client.get(prefix + "/status").json()["status"] == "transcribed"
    assert client.get(prefix + "/transcript").status_code == 200
    assert client.post(prefix + "/extract-action-items").status_code == 409
    assert client.get(prefix + "/action-items/status").json()["status"] == "ready"
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(prefix + "/action-items").json() == saved


def test_no_model_and_invalid_json_allow_retry(prepared):
    client, meeting_id, settings = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.post(prefix + "/extract-action-items").json()["detail"]["code"] == "llm_not_configured"
    assert client.get(prefix + "/action-items/status").json()["status"] == "not_started"
    class InvalidLLM:
        def generate_json(self, **kwargs):
            return '{"action_items": [{"description": "invented"}]}'
    client.app.state.extraction_service = ActionItemExtractionService(InvalidLLM(), settings)
    assert client.post(prefix + "/extract-action-items").status_code == 502
    assert client.get(prefix + "/action-items").status_code == 409
    use_stub(client, settings)
    assert client.post(prefix + "/extract-action-items").status_code == 200


def test_preconditions_and_concurrent_request(prepared):
    client, meeting_id, settings = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.get(prefix + "/action-items").status_code == 409
    assert client.post(f"/meetings/{uuid4()}/extract-action-items").status_code == 404
    other = client.post("/meetings", json={"title": "Not processed"}).json()["id"]
    assert client.post(f"/meetings/{other}/extract-action-items").status_code == 409
    class BlockingLLM(StubLLM):
        def generate_json(self, **kwargs):
            assert client.get(prefix + "/action-items/status").json()["status"] == "running"
            assert client.post(prefix + "/extract-action-items").status_code == 409
            return super().generate_json(**kwargs)
    client.app.state.extraction_service = ActionItemExtractionService(BlockingLLM(), settings)
    assert client.post(prefix + "/extract-action-items").status_code == 200
    assert client.get(f"/meetings/{other}/action-items").status_code == 409


def test_empty_transcript_needs_no_llm(prepared):
    client, meeting_id, settings = prepared
    path = settings.data_dir / "processed" / meeting_id / "transcript_raw.json"
    transcript = TranscriptRaw.model_validate_json(path.read_text(encoding="utf-8"))
    transcript.segments = []
    transcript.text = ""
    path.write_text(transcript.model_dump_json(), encoding="utf-8")
    response = client.post(f"/meetings/{meeting_id}/extract-action-items")
    assert response.status_code == 200
    assert response.json()["action_items"] == []


def test_real_loopback_transport_with_explicit_fake_server(prepared, monkeypatch):
    """Real HTTP integration, explicitly NOT a real LLM quality test."""
    client, meeting_id, settings = prepared
    from app.services.ollama import OllamaClient
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            paths.append(self.path)
            if self.path == "/api/show":
                assert "messages" not in body
                response = {"model_info": {"test.context_length": 32768}}
            else:
                assert self.path == "/api/chat"
                assert body["format"]["title"] == "ActionItemExtractionResult"
                response = {"done": True, "done_reason": "stop", "message": {"content": StubLLM().generate_json()}}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        llm_settings = Settings(_env_file=None, ollama_base_url=f"http://localhost:{server.server_port}", ollama_model="test-local")
        client.app.state.extraction_service = ActionItemExtractionService(OllamaClient(llm_settings), settings)
        assert client.post(f"/meetings/{meeting_id}/extract-action-items").status_code == 200
        assert paths == ["/api/show", "/api/chat"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
