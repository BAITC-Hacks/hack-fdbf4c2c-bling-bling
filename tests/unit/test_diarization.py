"""Contract tests with doubles; these do not measure model accuracy."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

from pydantic import ValidationError
import pytest

from app.core.config import Settings
from app.schemas.diarization import DiarizationSegment
from app.services.diarization import (
    DiarizationError, MODEL_FILES, PyannoteDiarizationService, local_pipeline_config,
)


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    for name in MODEL_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test-double-not-weights")
    config = {"pipeline": {"name": "pyannote.audio.pipelines.SpeakerDiarization", "params": {
        "segmentation": "$model/segmentation", "embedding": "$model/embedding", "plda": "$model/plda"}}}
    (tmp_path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    # JSON is valid YAML. Keep base test suite independent of optional ML deps.
    monkeypatch.setitem(sys.modules, "yaml", SimpleNamespace(safe_load=json.loads))
    return tmp_path


@pytest.mark.parametrize("device,available,expected", [("cpu", False, "cpu"), ("auto", False, "cpu"), ("auto", True, "cuda"), ("cuda", True, "cuda")])
def test_local_loading_device_and_offline(local_model, monkeypatch, device, available, expected):
    calls = []
    pipeline = SimpleNamespace(to=lambda target: calls.append(target))
    def load(config, token):
        assert token is False
        for part in ("segmentation", "embedding", "plda"):
            assert config["pipeline"]["params"][part] == {"checkpoint": str(local_model.resolve()), "subfolder": part}
        return pipeline
    constants = SimpleNamespace(HF_HUB_OFFLINE=False, HF_HUB_DISABLE_TELEMETRY=False)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(constants=constants))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available), device=str))
    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=SimpleNamespace(from_pretrained=load)))
    for name in ("PYANNOTE_METRICS_ENABLED", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.setenv(name, "1" if name == "PYANNOTE_METRICS_ENABLED" else "0")
    service = PyannoteDiarizationService(Settings(_env_file=None, diarization_model_path=local_model, diarization_device=device))
    assert service._load_pipeline() is pipeline
    assert service._load_pipeline() is pipeline
    assert calls == [expected]
    assert constants.HF_HUB_OFFLINE and constants.HF_HUB_DISABLE_TELEMETRY
    import os
    assert os.environ["PYANNOTE_METRICS_ENABLED"] == "0"


def test_explicit_cuda_does_not_fall_back(local_model, monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(constants=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "pyannote.audio", SimpleNamespace(Pipeline=None))
    for name in ("PYANNOTE_METRICS_ENABLED", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.setenv(name, "0")
    service = PyannoteDiarizationService(Settings(_env_file=None, diarization_model_path=local_model, diarization_device="cuda"))
    with pytest.raises(DiarizationError) as error:
        service.diarize(Path("unused.wav"))
    assert error.value.code == "diarization_cuda_unavailable"


@pytest.mark.parametrize("missing", MODEL_FILES)
def test_incomplete_model_never_loads(local_model, missing):
    (local_model / missing).unlink()
    with pytest.raises(DiarizationError) as error:
        local_pipeline_config(local_model)
    assert error.value.code == "diarization_model_not_ready"


def test_reject_lfs_and_cloud_config(local_model):
    checkpoint = local_model / "embedding/pytorch_model.bin"
    checkpoint.write_bytes(b"version https://git-lfs.github.com/spec/v1")
    with pytest.raises(DiarizationError):
        local_pipeline_config(local_model)
    checkpoint.write_bytes(b"fixture")
    (local_model / "config.yaml").write_text('{"pipeline": {"name": "CloudPipeline"}}')
    with pytest.raises(DiarizationError) as error:
        local_pipeline_config(local_model)
    assert error.value.code == "diarization_config_invalid"


def test_overlap_label_normalization_silence_and_retry(monkeypatch):
    service = PyannoteDiarizationService(Settings(_env_file=None))
    tracks = [(SimpleNamespace(start=1., end=3.), None, "b"),
              (SimpleNamespace(start=0., end=2.), None, "a"),
              (SimpleNamespace(start=4., end=5.), None, "a")]
    class Pipeline:
        def __call__(self, audio):
            assert audio == {"local": True}
            return SimpleNamespace(speaker_diarization=SimpleNamespace(itertracks=lambda yield_label: tracks))
    monkeypatch.setattr(service, "_load_pipeline", lambda: Pipeline())
    monkeypatch.setattr(service, "_load_audio", lambda path: {"local": True})
    result = service.diarize(Path("fixture.wav"))
    assert [item.speaker_id for item in result] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert result[1].start < result[0].end  # Overlap retained.
    tracks.clear()
    assert service.diarize(Path("silence.wav")) == []
    def fail():
        raise RuntimeError("private details")
    monkeypatch.setattr(service, "_load_pipeline", fail)
    with pytest.raises(DiarizationError) as error:
        service.diarize(Path("fixture.wav"))
    assert error.value.code == "diarization_failed"
    assert "private details" not in str(error.value)
    assert service._lock.acquire(blocking=False)
    try:
        with pytest.raises(DiarizationError) as busy:
            service.diarize(Path("fixture.wav"))
        assert busy.value.code == "diarization_busy"
    finally:
        service._lock.release()


@pytest.mark.parametrize("start,end", [(-1, 1), (2, 1), (1, 1), (0, float("inf")), (float("nan"), 1)])
def test_invalid_timestamps(start, end):
    with pytest.raises(ValidationError):
        DiarizationSegment(speaker_id="SPEAKER_00", start=start, end=end)


@pytest.mark.parametrize("rate,channels,frames", [(8000, 1, 10), (16000, 2, 10), (16000, 1, 0)])
def test_invalid_prepared_wav(tmp_path, monkeypatch, rate, channels, frames):
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    target = tmp_path / "audio.wav"
    with wave.open(str(target), "wb") as writer:
        writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0\0" * channels * frames)
    with pytest.raises(DiarizationError) as error:
        PyannoteDiarizationService._load_audio(target)
    assert error.value.code == "diarization_audio_invalid"


def test_env_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARIZATION_DEVICE", "cpu")
    monkeypatch.setenv("DIARIZATION_MODEL_PATH", str(tmp_path))
    config = Settings(_env_file=None)
    assert config.diarization_device == "cpu"
    assert config.diarization_model_path == tmp_path.resolve()
