"""Explicit one-time model download; never invoked while processing meetings."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.services.diarization import MODEL_FILES, MODEL_ID, local_pipeline_config


def main() -> int:
    try:
        from huggingface_hub import get_token, snapshot_download
        if not get_token():
            print("Примите условия на https://huggingface.co/" + MODEL_ID)
            print("Затем выполните hf auth login и повторите команду. Не добавляйте токен в Git или чат.")
            return 1
        directory = Settings().diarization_model_path
        snapshot_download(MODEL_ID, local_dir=directory, allow_patterns=list(MODEL_FILES), token=True)
        local_pipeline_config(directory)
        print(f"Модель сохранена локально: {directory}")
        return 0
    except ImportError:
        print("Установите requirements-diarization.txt в Python 3.11+.")
    except Exception:
        # HF exceptions may contain request details; do not print credentials.
        print("Не удалось подготовить модель. Проверьте доступ к Community-1, условия модели, сеть и свободное место.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
