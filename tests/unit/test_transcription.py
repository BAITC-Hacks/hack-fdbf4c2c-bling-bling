import sys
from types import SimpleNamespace as NS
import wave

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.transcription import FasterWhisperTranscriptionService, TranscriptionError


@pytest.fixture
def setup_stt(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (model_dir / name).write_text("test double")
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x00\x00" * 32000)
    calls = []
    output = NS(language="ru", text=" Проверка", words=True, cuda_count=0, fail=False, empty=False)

    class FakeModel:
        model = NS(is_multilingual=True)

        def __init__(self, path, **kwargs):
            calls.append(("load", path, kwargs))

        def transcribe(self, path, **kwargs):
            calls.append(("transcribe", path, kwargs))
            def generate():
                if output.fail:
                    raise RuntimeError("private model error")
                if output.empty:
                    return
                yield NS(id=0, text=output.text, start=0.1, end=1.2,
                         words=[NS(word=output.text, start=0.1, end=1.2, probability=0.8)] if output.words else None)
            return generate(), NS(language=output.language, language_probability=0.9, duration=2.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", NS(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "ctranslate2", NS(get_cuda_device_count=lambda: output.cuda_count,
                         get_supported_compute_types=lambda device: {"int8", "float32"} if device == "cpu" else {"float16", "int8_float16"}))
    settings = Settings(_env_file=None, whisper_model_path=model_dir)
    return settings, audio, calls, output


@pytest.mark.parametrize("language,text", [
    ("ru", " Подготовьте отчёт к пятнице."),
    ("kk", " Есепті жұмаға дейін дайындаңыз."),
    ("ru", " Коллеги, есепті жұмаға дейін дайындаңыз, пожалуйста."),
])
def test_preserve_language_and_timestamps(setup_stt, language, text):
    settings, audio, calls, output = setup_stt
    output.language, output.text = language, text
    result = FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert result.text == text.strip()
    assert result.segments[0].text == text
    assert result.segments[0].words[0].start == 0.1
    assert result.segments[0].end == 1.2
    assert result.detected_language == language
    assert result.language_scope == "initial_detection"
    assert calls[0][2]["local_files_only"] is True
    assert calls[0][1] == str(settings.local_whisper_path)
    assert calls[1][2]["task"] == "transcribe"
    assert calls[1][2]["language"] is None
    assert calls[1][2]["multilingual"] is True
    assert calls[1][2]["condition_on_previous_text"] is False


@pytest.mark.parametrize("device,count,expected,compute", [("cpu", 0, "cpu", "int8"),
    ("auto", 0, "cpu", "int8"), ("auto", 1, "cuda", "float16"), ("cuda", 1, "cuda", "float16")])
def test_device_selection(setup_stt, device, count, expected, compute):
    settings, audio, calls, output = setup_stt
    settings.stt_device, output.cuda_count = device, count
    result = FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert (result.device, result.compute_type) == (expected, compute)
    assert calls[0][2]["device"] == expected


def test_unavailable_cuda_is_not_silently_cpu(setup_stt):
    settings, audio, _, _ = setup_stt
    settings.stt_device = "cuda"
    with pytest.raises(TranscriptionError, match="CUDA") as caught:
        FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert caught.value.code == "cuda_unavailable"


def test_incompatible_compute_type(setup_stt):
    settings, audio, _, _ = setup_stt
    settings.stt_device, settings.stt_compute_type = "cpu", "float16"
    with pytest.raises(TranscriptionError) as caught:
        FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert caught.value.code == "unsupported_compute_type"


def test_missing_tokenizer_does_not_load_or_download(setup_stt):
    settings, audio, calls, _ = setup_stt
    (settings.local_whisper_path / "tokenizer.json").unlink()
    with pytest.raises(TranscriptionError) as caught:
        FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert caught.value.code == "model_not_ready"
    assert not calls


def test_lazy_failure_and_retry_releases_lock(setup_stt):
    settings, audio, calls, output = setup_stt
    service = FasterWhisperTranscriptionService(settings)
    output.fail = True
    with pytest.raises(TranscriptionError) as caught:
        service.transcribe(audio)
    assert caught.value.code == "transcription_failed"
    assert "private" not in caught.value.message
    output.fail, output.words = False, False
    assert service.transcribe(audio).segments[0].words is None
    assert sum(call[0] == "load" for call in calls) == 1


def test_busy_model(setup_stt):
    settings, audio, _, _ = setup_stt
    service = FasterWhisperTranscriptionService(settings)
    with service._lock, pytest.raises(TranscriptionError) as caught:
        service.transcribe(audio)
    assert caught.value.code == "stt_busy"


def test_env_names_and_legacy_precedence(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.setenv("HACKALEM_STT_MODEL", "medium")
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("WHISPER_COMPUTE_TYPE", "float32")
    settings = Settings(_env_file=None)
    assert (settings.stt_model, settings.stt_device, settings.stt_compute_type) == ("small", "cpu", "float32")


def test_language_configuration():
    assert Settings(_env_file=None, whisper_language="auto").whisper_language is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, whisper_language="ru")
    settings = Settings(_env_file=None, whisper_language="kk", whisper_multilingual=False)
    assert settings.whisper_language == "kk"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_model="tiny.en")


def test_silence_has_no_invented_language(setup_stt):
    settings, audio, _, output = setup_stt
    output.empty = True
    result = FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert result.text == ""
    assert result.segments == []
    assert result.detected_language is None
    assert result.language_probability is None
    assert result.warnings


def test_explicit_language_and_disabled_words(setup_stt):
    settings, audio, calls, _ = setup_stt
    settings.whisper_multilingual = False
    settings.whisper_language = "kk"
    settings.whisper_word_timestamps = False
    settings.whisper_chunk_length = 10
    FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert calls[1][2]["language"] == "kk"
    assert calls[1][2]["word_timestamps"] is False
    assert calls[1][2]["chunk_length"] == 10
    assert calls[1][2]["task"] == "transcribe"


def test_missing_dependency_is_explicit(setup_stt, monkeypatch):
    settings, audio, _, _ = setup_stt
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(TranscriptionError) as caught:
        FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert caught.value.code == "stt_dependency_missing"


def test_invalid_audio_before_model_load(setup_stt):
    settings, audio, calls, _ = setup_stt
    audio.write_bytes(b"invalid WAV")
    with pytest.raises(TranscriptionError) as caught:
        FasterWhisperTranscriptionService(settings).transcribe(audio)
    assert caught.value.code == "audio_unavailable"
    assert not calls
