from uuid import uuid4
from fastapi.testclient import TestClient
import pytest
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, database_path=tmp_path / "test.sqlite3")


def test_health_and_persistent_meeting(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        created = client.post("/meetings", json={"title": "  Кеңес / Совещание  "})
        assert created.status_code == 201
        meeting = created.json()
        assert meeting["title"] == "Кеңес / Совещание"
        assert meeting["status"] == "created"
        assert meeting["created_at"].endswith("Z")
        assert client.get(f"/meetings/{meeting['id']}").json() == meeting
        assert client.get(f"/meetings/{meeting['id']}/status").json() == {
            "id": meeting["id"], "status": "created",
        }
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/meetings/{meeting['id']}").json() == meeting


@pytest.mark.parametrize("payload", [{}, {"title": "  "}, {"title": "x" * 201},
                                         {"title": "demo", "status": "completed"}])
def test_invalid_creation(settings, payload):
    with TestClient(create_app(settings)) as client:
        assert client.post("/meetings", json=payload).status_code == 422


def test_missing_and_invalid_ids(settings):
    with TestClient(create_app(settings)) as client:
        for suffix in ("", "/status"):
            assert client.get(f"/meetings/{uuid4()}{suffix}").status_code == 404
            assert client.get(f"/meetings/not-a-uuid{suffix}").status_code == 422
        assert set(client.get("/openapi.json").json()["paths"]) == {
            "/health", "/meetings", "/meetings/{id}", "/meetings/{id}/status", "/meetings/{id}/upload",
            "/meetings/{id}/transcribe", "/meetings/{id}/transcript",
            "/meetings/{id}/diarize", "/meetings/{id}/diarization",
            "/meetings/{id}/diarization/status", "/meetings/{id}/speakers",
            "/meetings/{id}/extract-action-items", "/meetings/{id}/action-items",
            "/meetings/{id}/action-items/status", "/meetings/{id}/summarize",
            "/meetings/{id}/summary", "/meetings/{id}/summary/status", "/meetings/{id}/process",
            "/meetings/{id}/analysis", "/meetings/{id}/analyze", "/meetings/{id}/result",
            "/meetings/{id}/pipeline/status", "/meetings/{id}/exports/{format}"}


def test_optional_meeting_date_persists(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post("/meetings", json={"title": "Дата", "meeting_date": "2026-09-20"})
        assert response.status_code == 201
        meeting = response.json()
        assert meeting["meeting_date"] == "2026-09-20"
        assert client.post("/meetings", json={"title": "Дата", "meeting_date": "2026-02-30"}).status_code == 422
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/meetings/{meeting['id']}").json() == meeting


def test_local_frontend_resources(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Протокол совещания" in response.text
        assert "default-src 'self'" in response.headers["content-security-policy"]
        for resource in ("/static/app.js", "/static/styles.css"):
            assert client.get(resource).status_code == 200
        assert client.get("/static/../app/core/config.py").status_code == 404
