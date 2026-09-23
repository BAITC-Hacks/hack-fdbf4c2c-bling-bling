import io
from pathlib import Path
import subprocess
import wave
from uuid import uuid4

import imageio_ffmpeg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.config import Settings
from app.main import create_app
from app.models.audio import MeetingAudio
from app.models.meeting import Meeting


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data", database_path=tmp_path / "test.db",
                    ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe())


def wav_bytes(frames=800):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0\0\0" * frames)
    return stream.getvalue()


@pytest.mark.parametrize("extension,codec", [("wav", "pcm_s16le"), ("mp3", "libmp3lame"),
    ("m4a", "aac"), ("mp4", "aac"), ("webm", "libopus")])
def test_real_conversion_all_formats(settings, tmp_path, extension, codec):
    source = tmp_path / f"input.{extension}"
    command = [settings.ffmpeg_path, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.3"]
    if extension in {"mp4", "webm"}:
        command += ["-f", "lavfi", "-i", "color=size=16x16:duration=0.3",
                    "-c:v", "libx264" if extension == "mp4" else "libvpx-vp9", "-shortest"]
    command += ["-ac", "2", "-c:a", codec, str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Synthetic"}).json()["id"]
        response = client.post(f"/meetings/{meeting_id}/upload", files={"file": (f"../../input.{extension.upper()}", source.read_bytes())})
        assert response.status_code == 200, response.text
        assert response.json()["sample_rate"] == 16000
        assert response.json()["channels"] == 1
        assert response.json()["duration_seconds"] > 0
        assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "audio_ready"
        with client.app.state.session_factory() as session:
            stored = session.get(MeetingAudio, meeting_id)
            assert Path(stored.original_path).parent == settings.data_dir / "uploads"
            assert Path(stored.original_path).read_bytes() == source.read_bytes()
            with wave.open(stored.wav_path) as audio:
                assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
        assert client.post(f"/meetings/{meeting_id}/upload", files={"file": ("a.wav", wav_bytes())}).status_code == 409
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/meetings/{meeting_id}").json()["status"] == "audio_ready"


@pytest.mark.parametrize("name,body,status,code", [
    ("file.txt", b"x", 415, "unsupported_format"),
    ("file.wav", b"", 422, "empty_recording"),
    ("file.mp3", b"broken file", 422, "invalid_media"),
    ("file.wav", wav_bytes(0), 422, "empty_recording"),
    ("file.mp4", b"#EXTM3U\nhttp://example.com/audio.mp3", 422, "invalid_media"),
])
def test_errors_and_cleanup(settings, name, body, status, code):
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Demo"}).json()["id"]
        response = client.post(f"/meetings/{meeting_id}/upload", files={"file": (name, body)})
        assert response.status_code == status, response.text
        assert response.json()["detail"]["code"] == code
        assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "created"
        assert list((settings.data_dir / "uploads").iterdir()) == []
        assert list((settings.data_dir / "processed").iterdir()) == []
        with client.app.state.session_factory() as session:
            assert session.scalars(select(MeetingAudio)).all() == []
        assert client.post(f"/meetings/{meeting_id}/upload", files={"file": ("ok.wav", wav_bytes())}).status_code == 200


def test_missing_ffmpeg_and_limit(settings):
    settings.ffmpeg_path = "nonexistent-ffmpeg-123456"
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Demo"}).json()["id"]
        url = f"/meetings/{meeting_id}/upload"
        response = client.post(url, files={"file": ("a.wav", wav_bytes())})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "ffmpeg_not_found"
        settings.max_upload_bytes = 10
        assert client.post(url, files={"file": ("a.wav", wav_bytes())}).status_code == 413
        assert client.get(f"/meetings/{meeting_id}/status").json()["status"] == "created"


def test_no_audio_track(settings, tmp_path):
    source = tmp_path / "silent_video.mp4"
    subprocess.run([settings.ffmpeg_path, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=size=16x16:duration=0.1", "-an", str(source)], check=True, timeout=30)
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Demo"}).json()["id"]
        response = client.post(f"/meetings/{meeting_id}/upload", files={"file": (source.name, source.read_bytes())})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_media"


def test_unknown_and_busy_meeting(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/meetings/{uuid4()}/upload", files={"file": ("a.wav", wav_bytes())}).status_code == 404
        meeting_id = client.post("/meetings", json={"title": "Demo"}).json()["id"]
        with client.app.state.session_factory() as session:
            session.execute(update(Meeting).where(Meeting.id == meeting_id).values(status="uploading"))
            session.commit()
        assert client.post(f"/meetings/{meeting_id}/upload", files={"file": ("a.wav", wav_bytes())}).status_code == 409


def test_duration_limit(settings):
    settings.max_audio_seconds = 1
    with TestClient(create_app(settings)) as client:
        meeting_id = client.post("/meetings", json={"title": "Demo"}).json()["id"]
        response = client.post(f"/meetings/{meeting_id}/upload", files={"file": ("a.wav", wav_bytes(24000))})
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "recording_too_long"
