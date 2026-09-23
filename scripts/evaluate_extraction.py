"""Run semantic acceptance cases using REAL localhost inference, without mocks."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import ROOT, Settings
from app.schemas.action_item import ExtractionSegment
from app.services.extraction import ActionItemExtractionService, ExtractionError, normalized
from app.services.local_llm import LocalLLMError
from app.services.ollama import OllamaClient


def check_case(result, expected):
    problems = []
    if len(result.action_items) != len(expected):
        problems.append("action_count")
    used = set()
    for target in expected:
        matched = [i for i, item in enumerate(result.action_items) if i not in used
                   and target["anchor_id"] in item.source_segment_ids
                   and normalized(item.responsible or "") == normalized(target["responsible"] or "")
                   and normalized(item.deadline_raw or "") in [normalized(v or "") for v in target["deadline_allowed"]]]
        if len(matched) != 1:
            problems.append(f"assignment_at_segment_{target['anchor_id']}")
            continue
        used.add(matched[0])
        item = result.action_items[matched[0]]
        if item.decision_type != target["decision_type"]:
            problems.append("decision_type")
        if "condition" in target and normalized(item.condition or "") != normalized(target["condition"]):
            problems.append("condition")
        if not set(target.get("required_source_ids", [])) <= set(item.source_segment_ids):
            problems.append("missing_sources")
        expected_deadlines = sorted(normalized(v) for v in target["milestone_deadlines"])
        actual_deadlines = sorted(normalized(m.deadline_raw or "") for m in item.milestones)
        if expected_deadlines != actual_deadlines:
            problems.append("milestone_deadlines")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="?", type=Path, default=ROOT / "tests/fixtures/extraction_cases.json")
    args = parser.parse_args()
    settings = Settings()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    service = ActionItemExtractionService(OllamaClient(settings), settings)
    report = {"model": settings.ollama_model, "mode": "real_local_llm", "human_review_required": True, "cases": []}
    unavailable = False
    for case in cases:
        entry = {"name": case["name"]}
        try:
            result = service.extract([ExtractionSegment.model_validate(s) for s in case["segments"]])
            problems = check_case(result, case["expected"])
            entry.update(status="failed" if problems else "passed", problems=problems,
                         result=result.model_dump(mode="json"), sources=case["segments"])
        except (LocalLLMError, ExtractionError) as error:
            entry.update(status="error", code=error.code)
            unavailable = True
        report["cases"].append(entry)
        print(f"{entry['name']}: {entry['status']} {entry.get('code', '')}")
        if unavailable:
            # Do not repeatedly call an unavailable server or claim unrun cases passed.
            break
    report["not_run"] = len(cases) - len(report["cases"])
    target = settings.data_dir / "extraction-evaluation" / "report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Local report: {target}")
    return 2 if unavailable else int(any(entry["status"] != "passed" for entry in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
