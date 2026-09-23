"""Explicit online model preparation. Never receives meeting audio/text."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import Settings
from app.services.transcription import validate_model_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override WHISPER_MODEL for this download")
    parser.add_argument("--output", type=Path, help="Override WHISPER_MODEL_PATH")
    args = parser.parse_args()
    overrides = {}
    if args.model:
        overrides["stt_model"] = args.model
    if args.output:
        overrides["whisper_model_path"] = args.output
    settings = Settings(**overrides)
    from faster_whisper.utils import download_model
    download_model(settings.stt_model, output_dir=str(settings.local_whisper_path))
    validate_model_directory(settings.local_whisper_path)
    print(f"Model ready: {settings.local_whisper_path}")


if __name__ == "__main__":
    main()
