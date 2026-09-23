"""Explicit manual evaluation of both supplied protocols on a REAL localhost LLM.

Not imported/called by unit tests. No model is used as a judge; review locally.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import ROOT, Settings
from app.core.logging import configure_logging
from app.schemas.action_item import ExtractionSegment
from app.services.analysis import LocalMeetingAnalyzer
from app.services.local_llm import LocalLLMError
from app.services.ollama import OllamaClient

CHECKLIST = [
    "Responsible is not automatically the speaker/chair",
    "Ерлан did not speak; recap does not reassign his claim to Ботагоз",
    "юридический департамент is a valid responsible department",
    "Audit deadline changed from two weeks to three weeks / fifteenth October",
    "Салтанат has no task deadline; five working days is a contract clause",
    "Relative deadlines remain relative; do not invent a year/date",
    "Recap does not duplicate tasks or lose milestone deadlines",
    "Termination of the contract remains conditional",
    "A proposal becomes an assignment only when accepted",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true", help="Explicitly allow real inference on localhost")
    parser.add_argument("--protocol", choices=["1", "2", "both"], default="both")
    parser.add_argument("--include-proposal-window", action="store_true",
                        help="Also analyze protocol 1 turn 21 alone before acceptance (one extra independent input)")
    args = parser.parse_args()
    if not args.run_local:
        print("No inference performed. Use --run-local after preparing Ollama and a local model.")
        return 0
    settings = Settings()
    configure_logging(settings.log_level)
    data = json.loads((ROOT / "tests/fixtures/meeting_protocols.json").read_text(encoding="utf-8"))
    selected = [p for p in data["protocols"] if args.protocol == "both" or p["id"] == f"protocol_{args.protocol}"]
    cases = [{"name": p["id"], "segments": p["segments"], "reference": p["mock_response"]} for p in selected]
    if args.include_proposal_window:
        cases.append({"name": "protocol_1_proposal_before_acceptance", "segments": [data["protocols"][0]["segments"][21]],
                      "reference": {"action_items": [], "summary": {"decisions": []}}})
    analyzer = LocalMeetingAnalyzer(OllamaClient(settings, server_context_check=True), settings)
    report = {"model": settings.ollama_model, "mode": "real_local_llm", "human_review_required": True,
              "checklist": CHECKLIST, "reference_notes": data["reference_notes"], "cases": []}
    failed = False
    for case in cases:
        entry = {"name": case["name"]}
        try:
            # Only speech turns are sent. Never feed mock answers or the supplied summaries into inference.
            result = analyzer.analyze([ExtractionSegment.model_validate(s) for s in case["segments"]])
            entry.update(status="needs_human_review", result=result.model_dump(mode="json"),
                         hand_authored_reference=case["reference"])
        except LocalLLMError as error:
            entry.update(status="error", code=error.code)
            failed = True
        report["cases"].append(entry)
        print(f"{case['name']}: {entry['status']} {entry.get('code', '')}")
        if failed:
            break  # Do not spend repeated calls on an unavailable or invalid model.
    report["not_run"] = len(cases) - len(report["cases"])
    directory = settings.data_dir / "protocol-evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Local report: {path}")
    print("Generation success is not semantic acceptance. Review all checklist items against speech turns.")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
