"""HTTP and persistence contracts for local diarization."""
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.api.diarization import get_diarization_service
from app.core.config import Settings
from app.main import create_app
from app.models.audio import MeetingAudio
from app.models.meeting import Meeting
from app.schemas.diarization import DiarizationSegment
from app.services.diarization import DiarizationError


class StubDiarization:
    """Contract double, not real inference."""
    def diarize(self, path):
        assert path.is_file()
        return [DiarizationSegment(speaker_id="SPEAKER_00", start=0, end=2),
                DiarizationSegment(speaker_id="SPEAKER_01", start=1, end=3)]


@pytest.fixture
def prepared(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path, database_path=tmp_path / "test.sqlite3",
                      diarization_model_path=tmp_path / "missing-model")
    app = create_app(config)
    with TestClient(app) as client:
        meeting_id = client.post("/meetings", json={"title": "Кеңес"}).json()["id"]
        audio = tmp_path / "processed" / "contract-fixture.wav"
        audio.write_bytes(b"contract double does not decode WAV")
        with app.state.session_factory() as session:
            session.add(MeetingAudio(meeting_id=meeting_id, original_path=str(audio), wav_path=str(audio), duration_seconds=3))
            session.get(Meeting, meeting_id).status = "audio_ready"
            session.commit()
        yield client, meeting_id, config


def test_result_names_and_restart(prepared):
    client, meeting_id, config = prepared
    prefix = f"/meetings/{meeting_id}"
    client.app.dependency_overrides[get_diarization_service] = lambda: StubDiarization()
    assert client.get(prefix + "/diarization/status").json()["status"] == "not_started"
    result = client.post(prefix + "/diarize")
    assert result.status_code == 200, result.text
    target = config.data_dir / "processed" / meeting_id / "diarization.json"
    assert json.loads(target.read_text()) == result.json()
    assert client.get(prefix + "/status").json()["status"] == "audio_ready"
    assert client.get(prefix + "/diarization/status").json()["status"] == "ready"
    assert all(item["name"] is None for item in client.get(prefix + "/speakers").json())
    mapping = {"names": {"SPEAKER_00": "Асхат Ерланович", "SPEAKER_01": "Гульмира Сериковна"}}
    response = client.put(prefix + "/speakers", json=mapping)
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Асхат Ерланович"
    assert client.post(prefix + "/diarize").status_code == 409
    assert json.loads(target.read_text()) == result.json()  # Manual names do not alter raw output.
    with TestClient(create_app(config)) as restarted:
        assert restarted.get(prefix + "/diarization").json() == result.json()
        assert restarted.get(prefix + "/speakers").json() == response.json()
        assert restarted.put(prefix + "/speakers", json={"names": {}}).json()[0]["name"] is None


def test_missing_weights_and_failure_retry(prepared):
    client, meeting_id, config = prepared
    prefix = f"/meetings/{meeting_id}"
    response = client.post(prefix + "/diarize")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "diarization_model_not_ready"
    assert client.get(prefix + "/diarization/status").json()["status"] == "not_started"
    class Failing:
        def diarize(self, path):
            raise DiarizationError("diarization_failed", "Ошибка", 500)
    client.app.dependency_overrides[get_diarization_service] = lambda: Failing()
    assert client.post(prefix + "/diarize").status_code == 500
    client.app.dependency_overrides[get_diarization_service] = lambda: StubDiarization()
    assert client.post(prefix + "/diarize").status_code == 200


def test_preconditions_and_mapping_validation(prepared):
    client, meeting_id, _ = prepared
    prefix = f"/meetings/{meeting_id}"
    assert client.get(prefix + "/diarization").status_code == 409
    assert client.get(prefix + "/speakers").status_code == 409
    assert client.post(f"/meetings/{uuid4()}/diarize").status_code == 404
    new_id = client.post("/meetings", json={"title": "No audio"}).json()["id"]
    assert client.post(f"/meetings/{new_id}/diarize").status_code == 409
    client.app.dependency_overrides[get_diarization_service] = lambda: StubDiarization()
    assert client.post(prefix + "/diarize").status_code == 200
    for names in ({"SPEAKER_99": "Иван"}, {"SPEAKER_00": "  "}, {"SPEAKER_00": "x" * 201}, {"invalid": "Имя"}):
        assert client.put(prefix + "/speakers", json={"names": names}).status_code == 422
    assert client.get(f"/meetings/{new_id}/speakers").status_code == 409


def test_storage_failure_cleans_up(prepared, monkeypatch):
    client, meeting_id, config = prepared
    client.app.dependency_overrides[get_diarization_service] = lambda: StubDiarization()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", fail)
    assert client.post(f"/meetings/{meeting_id}/diarize").status_code == 507
    assert not list(config.data_dir.rglob("*.tmp"))
    assert not list(config.data_dir.rglob("diarization.json"))
    assert client.get(f"/meetings/{meeting_id}/diarization/status").json()["status"] == "not_started"


def test_concurrent_claim_and_existing_transcript(prepared):
    client, meeting_id, config = prepared
    prefix = f"/meetings/{meeting_id}"
    # A valid existing STT result must remain readable after diarization.
    from app.schemas.transcript import TranscriptRaw
    transcript = TranscriptRaw(model="fixture", device="cpu", compute_type="int8", text="",
                               duration_seconds=3, options={}, segments=[])
    target = config.data_dir / "processed" / meeting_id / "transcript_raw.json"
    target.parent.mkdir(parents=True)
    target.write_text(transcript.model_dump_json(), encoding="utf-8")
    with client.app.state.session_factory() as session:
        session.get(Meeting, meeting_id).status = "transcribed"
        session.commit()
    class Checking(StubDiarization):
        def diarize(self, path):
            assert client.get(prefix + "/diarization/status").json()["status"] == "running"
            assert client.post(prefix + "/diarize").status_code == 409
            return super().diarize(path)
    client.app.dependency_overrides[get_diarization_service] = lambda: Checking()
    assert client.post(prefix + "/diarize").status_code == 200
    assert client.get(prefix + "/transcript").json() == transcript.model_dump(mode="json")
    assert client.get(prefix + "/status").json()["status"] == "transcribed"
