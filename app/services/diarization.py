"""Local Community-1 inference. No downloads, cloud API, or identity recognition."""
import os
from pathlib import Path
from threading import Lock
from typing import Protocol
import wave

from app.core.config import Settings
from app.schemas.diarization import DiarizationSegment

MODEL_ID = "pyannote/speaker-diarization-community-1"
MODEL_FILES = (
    "config.yaml", "segmentation/pytorch_model.bin", "embedding/pytorch_model.bin",
    "plda/plda.npz", "plda/xvec_transform.npz",
)


class DiarizationError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class DiarizationService(Protocol):
    def diarize(self, audio_path: Path) -> list[DiarizationSegment]: ...


def enable_offline_inference() -> None:
    # Force, rather than setdefault: privacy is not an optional configuration.
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    # huggingface_hub may already have been imported by faster-whisper.
    from huggingface_hub import constants
    constants.HF_HUB_OFFLINE = True
    constants.HF_HUB_DISABLE_TELEMETRY = True


def local_pipeline_config(directory: Path) -> dict:
    for name in MODEL_FILES:
        asset = directory / name
        if not asset.is_file() or asset.stat().st_size == 0:
            raise DiarizationError("diarization_model_not_ready", "Сначала скачайте полный набор файлов Community-1 через scripts/prepare_diarization.py.")
        with asset.open("rb") as source:
            if source.read(80).startswith(b"version https://git-lfs.github.com/spec/"):
                raise DiarizationError("diarization_model_not_ready", "Вместо весов обнаружены указатели Git LFS. Скачайте настоящие файлы модели.")
    import yaml
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("pipeline", {}).get("name") != "pyannote.audio.pipelines.SpeakerDiarization" or "preprocessors" in config:
        raise DiarizationError("diarization_config_invalid", "Ожидается локальная конфигурация Community-1 без внешних preprocessors.")
    # Explicit local assets also replace constructor defaults pointing at HF.
    params = config["pipeline"].setdefault("params", {})
    for component in ("segmentation", "embedding", "plda"):
        params[component] = {"checkpoint": str(directory.resolve()), "subfolder": component}
    params.pop("token", None)
    params.pop("cache_dir", None)
    config.pop("device", None)  # ENV is the single source of device selection.
    return config


class PyannoteDiarizationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pipeline = None
        self._lock = Lock()

    def _load_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        try:
            config = local_pipeline_config(self.settings.diarization_model_path)
            enable_offline_inference()
            import torch
            from pyannote.audio import Pipeline

            device = self.settings.diarization_device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cuda" and not torch.cuda.is_available():
                raise DiarizationError("diarization_cuda_unavailable", "CUDA недоступна. Установите совместимый PyTorch или выберите DIARIZATION_DEVICE=cpu.")
            pipeline = Pipeline.from_pretrained(config, token=False)
            if pipeline is None:
                raise RuntimeError("No pipeline returned")
            pipeline.to(torch.device(device))
            self._pipeline = pipeline
            return pipeline
        except DiarizationError:
            raise
        except ImportError:
            raise DiarizationError("diarization_dependency_missing", "Установите requirements-diarization.txt в окружение Python 3.11+.") from None
        except Exception:
            raise DiarizationError("diarization_model_load_failed", "Не удалось загрузить локальную модель диаризации. Проверьте комплектность весов и совместимость PyTorch.") from None

    @staticmethod
    def _load_audio(audio_path: Path):
        # Read prepared PCM directly, avoiding torchcodec/FFmpeg DLL requirements.
        try:
            import numpy as np
            import torch
            with wave.open(str(audio_path), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("Expected mono PCM16 16kHz")
                frame_count = source.getnframes()
                pcm = source.readframes(frame_count)
            if not frame_count or len(pcm) != frame_count * 2:
                raise ValueError("Empty or truncated WAV")
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": 16000}
        except (OSError, ValueError, EOFError, wave.Error):
            raise DiarizationError("diarization_audio_invalid", "Подготовленный WAV отсутствует, пуст или повреждён. Нужен mono PCM16 16 kHz.", 422) from None

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        if not self._lock.acquire(blocking=False):
            raise DiarizationError("diarization_busy", "Модель обрабатывает другую запись. Повторите позже.", 409)
        try:
            pipeline = self._load_pipeline()
            audio = self._load_audio(audio_path)
            output = pipeline(audio)
            # Preserve overlapping speech; do not substitute exclusive diarization.
            tracks = sorted(output.speaker_diarization.itertracks(yield_label=True),
                            key=lambda item: (item[0].start, item[0].end, str(item[2])))
            labels: dict[str, str] = {}
            result = []
            for segment, _, label in tracks:
                key = str(label)
                speaker = labels.setdefault(key, f"SPEAKER_{len(labels):02d}")
                result.append(DiarizationSegment(speaker_id=speaker, start=segment.start, end=segment.end))
            return result
        except DiarizationError:
            raise
        except Exception:
            raise DiarizationError("diarization_failed", "Не удалось выполнить локальную диаризацию.", 500) from None
        finally:
            self._lock.release()
