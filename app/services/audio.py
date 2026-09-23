"""Local-only upload and PCM conversion. No network or model calls."""
from pathlib import Path
import shutil
import subprocess
import wave
from typing import BinaryIO

from app.core.config import Settings

FORMATS = {".wav": "wav", ".mp3": "mp3", ".m4a": "mov", ".mp4": "mov", ".webm": "matroska"}


class AudioError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def save_upload(stream: BinaryIO, destination: Path, limit: int) -> None:
    size = 0
    with destination.open("xb") as target:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise AudioError("file_too_large", "Файл превышает допустимый размер.", 413)
            target.write(chunk)
    if not size:
        raise AudioError("empty_recording", "Загружена пустая запись.")


def convert_audio(source: Path, destination: Path, settings: Settings) -> float:
    executable = shutil.which(settings.ffmpeg_path)
    if executable is None:
        raise AudioError("ffmpeg_not_found", "ffmpeg не найден. Установите его или задайте HACKALEM_FFMPEG_PATH.", 503)
    # Force the allowed demuxer: renamed playlists cannot trigger network access.
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
               "-protocol_whitelist", "file", "-f", FORMATS[source.suffix],
               "-i", str(source), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
               "-t", str(settings.max_audio_seconds + 1), "-f", "wav", "-y", str(destination)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=settings.ffmpeg_timeout_seconds,
                                check=False)
    except FileNotFoundError:
        raise AudioError("ffmpeg_not_found", "ffmpeg не найден.", 503) from None
    except subprocess.TimeoutExpired:
        raise AudioError("conversion_timeout", "Превышено время подготовки аудио.", 504) from None
    except OSError:
        raise AudioError("ffmpeg_unavailable", "Не удалось запустить ffmpeg.", 503) from None
    if result.returncode:
        raise AudioError("invalid_media", "Файл повреждён, не соответствует формату или не содержит аудиодорожки.")
    try:
        with wave.open(str(destination), "rb") as audio:
            frames = audio.getnframes()
            if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2:
                raise AudioError("invalid_output", "ffmpeg вернул некорректный WAV.", 500)
            duration = frames / 16000
    except (wave.Error, EOFError, OSError):
        raise AudioError("invalid_media", "Не удалось прочитать подготовленное аудио.") from None
    if not frames:
        raise AudioError("empty_recording", "Запись не содержит аудиосэмплов.")
    if duration > settings.max_audio_seconds:
        raise AudioError("recording_too_long", "Запись превышает допустимую длительность.", 413)
    return duration
