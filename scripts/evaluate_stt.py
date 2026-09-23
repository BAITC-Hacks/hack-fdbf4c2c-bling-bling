"""Offline evaluation on a local JSON manifest; never downloads or uploads audio."""
import argparse
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import Settings
from app.services.transcription import FasterWhisperTranscriptionService


def word_error_rate(reference: str, hypothesis: str) -> float:
    expected = re.findall(r"\w+", reference.lower())
    actual = re.findall(r"\w+", hypothesis.lower())
    previous = list(range(len(actual) + 1))
    for i, word in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / max(1, len(expected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help='JSON list: [{"name":"ru","audio":"ru.wav","reference":"..."}]')
    parser.add_argument("--output", type=Path, default=Path("data/stt-evaluation/report.json"))
    args = parser.parse_args()
    settings = Settings()
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))

    # Fail on Python socket connection attempts; this is not an OS network sandbox.
    def deny_network(event, arguments):
        if event == "socket.connect":
            raise RuntimeError("Network access forbidden during STT evaluation")
    sys.addaudithook(deny_network)
    service = FasterWhisperTranscriptionService(settings)
    report = []
    for case in cases:
        started = time.monotonic()
        result = service.transcribe((args.manifest.parent / case["audio"]).resolve())
        elapsed = time.monotonic() - started
        report.append({"name": case["name"], "elapsed_seconds": round(elapsed, 2),
                       "reference": case["reference"], "wer": word_error_rate(case["reference"], result.text),
                       "transcript": result.model_dump()})
        print(f"{case['name']}: {elapsed:.2f}s, WER={report[-1]['wer']:.3f}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
