import json
from pathlib import Path
from uuid import uuid4
import wave

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import update

from app.api.transcripts import get_transcription_service
from app.core.config import Settings
from app.main import create_app
from app.models.audio import MeetingAudio
from app.models.meeting import Meeting
from app.schemas.transcript import TranscriptRaw, TranscriptSegment
from app.services.transcription import TranscriptionError


class StubTranscriptionService:
    """Contract double only: never used by the production application."""
    def transcribe(self, audio_path):
        assert audio_path.is_file()
        return TranscriptRaw(model="test-fixture", device="cpu", compute_type="int8",
                             text="Есеп дайын. Отчёт готов.", detected_language="kk",
                             duration_seconds=1, options={"task": "transcribe"},
                             segments=[TranscriptSegment(id=0, start=0, end=1, text="Есеп дайын. Отчёт готов.")])


@pytest.fixture
def prepared(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_path=tmp_path / "db.sqlite3",
                        whisper_model_path=tmp_path / "missing-model")
    application = create_app(settings)
    with TestClient(application) as client:
        meeting_id = client.post("/meetings", json={"title": "Synthetic"}).json()["id"]
        audio_path = tmp_path / "processed" / "fixture.wav"
        with wave.open(str(audio_path), "wb") as writer:
            writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\0\0" * 16000)
        with application.state.session_factory() as session:
            session.add(MeetingAudio(meeting_id=meeting_id, original_path=str(audio_path),
                                     wav_path=str(audio_path), duration_seconds=1))
            session.execute(update(Meeting).where(Meeting.id == meeting_id).values(status="audio_ready"))
            session.commit()
        yield client, meeting_id, settings


def test_transcription_persists_and_reloads(prepared):
    client, meeting_id, settings = prepared
    client.app.dependency_overrides[get_transcription_service] = lambda: StubTranscriptionService()
    response = client.post(f"/meetings/{meeting_id}/transcribe")
    assert response.status_code == 200, response.text
    target = settings.data_dir / "processed" / meeting_id / "transcript_raw.json"
    assert json.loads(target.read_text(encoding="utf-8")) == response.json()
    assert "Есеп дайын" in target.read_text(encoding="utf-8")
    assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "transcribed"
    assert client.post(f"/meetings/{meeting_id}/transcribe").status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/meetings/{meeting_id}/transcript").json() == response.json()


def test_missing_weights_preserves_audio_and_allows_retry(prepared):
    client, meeting_id, settings = prepared
    response = client.post(f"/meetings/{meeting_id}/transcribe")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "model_not_ready"
    assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "audio_ready"
    assert (settings.data_dir / "processed" / "fixture.wav").exists()
    assert not list(settings.data_dir.rglob("transcript_raw.json"))
    client.app.dependency_overrides[get_transcription_service] = lambda: StubTranscriptionService()
    assert client.post(f"/meetings/{meeting_id}/transcribe").status_code == 200


def test_preconditions(prepared):
    client, meeting_id, settings = prepared
    assert client.get(f"/meetings/{meeting_id}/transcript").status_code == 409
    assert client.post(f"/meetings/{uuid4()}/transcribe").status_code == 404
    new_id = client.post("/meetings", json={"title": "No audio"}).json()["id"]
    assert client.post(f"/meetings/{new_id}/transcribe").json()["detail"]["code"] == "audio_not_ready"
    with client.app.state.session_factory() as session:
        session.execute(update(Meeting).where(Meeting.id == meeting_id).values(status="transcribing"))
        session.commit()
    assert client.post(f"/meetings/{meeting_id}/transcribe").status_code == 409


def test_storage_failure_resets_status(prepared, monkeypatch):
    client, meeting_id, settings = prepared
    client.app.dependency_overrides[get_transcription_service] = lambda: StubTranscriptionService()
    def fail(*args, **kwargs):
        raise OSError("Disk unavailable")
    monkeypatch.setattr(Path, "replace", fail)
    assert client.post(f"/meetings/{meeting_id}/transcribe").status_code == 507
    assert not list(settings.data_dir.rglob("*.tmp"))
    assert not list(settings.data_dir.rglob("transcript_raw.json"))
    assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "audio_ready"


def test_service_failure_and_busy(prepared):
    client, meeting_id, _ = prepared
    class BusyService:
        def transcribe(self, path):
            raise TranscriptionError("stt_busy", "Занято", 409)
    client.app.dependency_overrides[get_transcription_service] = lambda: BusyService()
    assert client.post(f"/meetings/{meeting_id}/transcribe").status_code == 409
    assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "audio_ready"
