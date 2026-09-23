"""Local STT adapter. Downloads belong exclusively to the preparation script."""
from pathlib import Path
from threading import Lock
from typing import Protocol
import wave

from app.core.config import Settings
from app.schemas.transcript import TranscriptRaw, TranscriptSegment, WordTimestamp

REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class TranscriptionService(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptRaw: ...


def validate_model_directory(path: Path) -> None:
    vocabulary_exists = any((path / name).is_file() for name in ("vocabulary.json", "vocabulary.txt"))
    if not vocabulary_exists or any(not (path / name).is_file() for name in REQUIRED_MODEL_FILES):
        raise TranscriptionError("model_not_ready", "Локальная модель неполная или отсутствует. Выполните scripts/prepare_whisper.py до обработки.")


class FasterWhisperTranscriptionService:
    def __init__(self, settings: Settings):
        self.settings = settings.model_copy(deep=True)
        self._model = None
        self._lock = Lock()
        self._device = settings.stt_device
        self._compute_type = settings.stt_compute_type

    def _load_model(self):
        if self._model is not None:
            return self._model
        validate_model_directory(self.settings.local_whisper_path)
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
        except ImportError:
            raise TranscriptionError("stt_dependency_missing", "Установите requirements-stt.txt.") from None
        try:
            self._device = self.settings.stt_device
            if self._device == "auto":
                self._device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
            if self._device == "cuda" and not ctranslate2.get_cuda_device_count():
                raise TranscriptionError("cuda_unavailable", "CUDA недоступна. Задайте WHISPER_DEVICE=cpu или настройте NVIDIA CUDA.")
            self._compute_type = self.settings.stt_compute_type
            if self._compute_type == "auto":
                self._compute_type = "float16" if self._device == "cuda" else "int8"
            if self._compute_type not in ctranslate2.get_supported_compute_types(self._device):
                raise TranscriptionError("unsupported_compute_type", "WHISPER_COMPUTE_TYPE не поддерживается выбранным устройством.")
            model = WhisperModel(
                str(self.settings.local_whisper_path), device=self._device,
                compute_type=self._compute_type, cpu_threads=self.settings.whisper_cpu_threads,
                local_files_only=True,
            )
            if not model.model.is_multilingual:
                raise TranscriptionError("english_only_model", "Нужна многоязычная модель Whisper.")
            self._model = model
            return model
        except TranscriptionError:
            raise
        except Exception:
            raise TranscriptionError("model_load_failed", "Не удалось загрузить локальную модель. Проверьте веса, память, CUDA и compute type.") from None

    def transcribe(self, audio_path: Path) -> TranscriptRaw:
        if not self._lock.acquire(blocking=False):
            raise TranscriptionError("stt_busy", "Модель занята другой записью. Повторите запрос позже.", 409)
        try:
            try:
                with wave.open(str(audio_path), "rb") as audio:
                    if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
                        raise TranscriptionError("invalid_stt_audio", "Нужен подготовленный WAV mono 16 kHz PCM16.", 422)
                    if audio.getnframes() == 0:
                        raise TranscriptionError("empty_recording", "Запись не содержит аудиосэмплов.", 422)
            except (OSError, EOFError, wave.Error):
                raise TranscriptionError("audio_unavailable", "Подготовленный WAV отсутствует или повреждён.", 422) from None
            model = self._load_model()
            options = dict(
                task="transcribe", language=self.settings.whisper_language,
                multilingual=self.settings.whisper_multilingual,
                word_timestamps=self.settings.whisper_word_timestamps,
                vad_filter=self.settings.whisper_vad_filter,
                beam_size=self.settings.whisper_beam_size,
                chunk_length=self.settings.whisper_chunk_length,
                condition_on_previous_text=self.settings.whisper_condition_on_previous_text,
            )
            try:
                raw_segments, info = model.transcribe(str(audio_path), **options)
                # The generator performs inference lazily; consume it inside the lock/error boundary.
                segments = [TranscriptSegment(
                    id=segment.id, text=segment.text, start=segment.start, end=segment.end,
                    words=None if segment.words is None else [WordTimestamp(
                        text=word.word, start=word.start, end=word.end, probability=word.probability,
                    ) for word in segment.words],
                ) for segment in raw_segments]
                return TranscriptRaw(
                    model=self.settings.stt_model, device=self._device, compute_type=self._compute_type,
                    text="".join(segment.text for segment in segments).strip(), segments=segments,
                    detected_language=info.language if segments else None,
                    language_probability=info.language_probability if segments else None,
                    duration_seconds=info.duration, options=options,
                    warnings=[] if segments else ["Речь не обнаружена; проверьте запись и настройки VAD."],
                )
            except Exception:
                raise TranscriptionError("transcription_failed", "Ошибка локального распознавания. Проверьте память, устройство и совместимость модели.", 500) from None
        finally:
            self._lock.release()
