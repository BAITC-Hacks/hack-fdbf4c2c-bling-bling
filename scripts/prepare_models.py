"""Run only during installation on an empty, network-enabled model volume."""
import hashlib
import json
import os
import urllib.request
from pathlib import Path

os.environ.pop('HF_HUB_OFFLINE', None)
from huggingface_hub import snapshot_download

root = Path('/models')
root.mkdir(parents=True, exist_ok=True)
snapshot_download('Systran/faster-whisper-small', local_dir=root / 'whisper-small', allow_patterns=['*.json', '*.bin', '*.txt'])
url = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx'
speaker = root / 'speaker.onnx'
if not speaker.exists():
    urllib.request.urlretrieve(url, speaker.with_suffix('.partial'))
    speaker.with_suffix('.partial').replace(speaker)
manifest = {'asr': 'Systran/faster-whisper-small', 'speaker_url': url, 'speaker_sha256': hashlib.sha256(speaker.read_bytes()).hexdigest()}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps(manifest, indent=2))
