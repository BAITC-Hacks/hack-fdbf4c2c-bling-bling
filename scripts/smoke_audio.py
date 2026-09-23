"""Real offline speech smoke. Input and transcript stay on the local machine."""
import argparse
import json
import time
from pathlib import Path
from backend.app.speech import transcribe

parser = argparse.ArgumentParser()
parser.add_argument('recording', type=Path)
parser.add_argument('--report', type=Path, default=Path('/data/audio-smoke-report.json'))
args = parser.parse_args()
started = time.monotonic()
result = transcribe(args.recording)
segments = result['segments']
assert segments and all(s['text'] for s in segments)
assert all(0 <= s['start_ms'] < s['end_ms'] for s in segments)
assert all(s['timing_source'] == 'asr' for s in segments)
speakers = sorted({s['speaker'] for s in segments})
assert any(s != 'SPEAKER_UNKNOWN' for s in speakers)
report = {'passed': True, 'mode': 'real_local_models', 'seconds': round(time.monotonic()-started, 2),
          'segment_count': len(segments), 'speakers': speakers, 'language': result['language'],
          'last_end_ms': segments[-1]['end_ms'], 'diarization': result['diarization'],
          'asr_model': result['asr_model'], 'quality_evaluated': False}
args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
