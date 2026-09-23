import subprocess
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.services.audio import AudioError, convert_audio


def test_timeout_is_explicit_and_network_is_disabled(tmp_path):
    source, output = tmp_path / "a.mp4", tmp_path / "b.wav"
    settings = Settings(_env_file=None, ffmpeg_timeout_seconds=2)
    with patch("app.services.audio.shutil.which", return_value="ffmpeg"), patch(
        "app.services.audio.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 2)
    ) as run:
        with pytest.raises(AudioError) as caught:
            convert_audio(source, output, settings)
        assert caught.value.code == "conversion_timeout"
        assert caught.value.status == 504
        command = run.call_args.args[0]
        assert command[command.index("-protocol_whitelist") + 1] == "file"
        assert command[command.index("-f") + 1] == "mov"
        assert run.call_args.kwargs["timeout"] == 2
