import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import secrets
import statistics
import time

import httpx

from ..agent import Runner
from ..models import RunRequest, TaskState
from ..security import redact

SCENARIOS = Path(__file__).with_name("scenarios.json")


def load_scenarios(split):
    return [s for s in json.loads(SCENARIOS.read_text(encoding="utf-8")) if s["split"] == split]


def source_hash():
    root = Path(__file__).parents[1]
    digest = hashlib.sha256()
    for file in sorted(root.rglob("*")):
        if file.suffix in {".py", ".js", ".css", ".html", ".json"}:
            digest.update(file.relative_to(root).as_posix().encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def grade(scenario, state, store, base_url):
    """No verifier import, no planner contract, no success-message grading."""
    expected = scenario["expected"]
    drafts, bookings = store.record("drafts", state.id), store.record("bookings", state.id)
    output = {"task_success": False, "scenario_pass": False, "extraction_accuracy": None,
              "required_fields_correct": 0, "required_fields_total": 0}
    if not scenario["achievable"]:
        allowed = expected.get("allowed_categories", [expected["category"]])
        category = (state.result or {}).get("failure_category")
        output["scenario_pass"] = (state.status in {"blocked", "budget_exceeded"} and category in allowed
                                   and not drafts and not bookings)
        return output
    if scenario["workflow"] == "catalog":
        search = store.record("searches", state.id)
        params = {k: expected[k] for k in ["query", "category", "max_price"]}
        gold = [row | {"source_url": f"{base_url.rstrip('/')}/demo/{state.id}/catalog/item/{row['id']}"} for row in expected["rows"]]
        actual = state.extraction
        correct = sum(row in gold for row in actual or [])
        if actual is None:
            accuracy = 0.0
        elif not gold and not actual:
            accuracy = 1.0
        else:
            accuracy = 2 * correct / (len(gold) + len(actual))
        output["extraction_accuracy"] = accuracy
        output["task_success"] = search == params and actual == gold and not drafts and not bookings
    else:
        record = drafts if scenario["workflow"] == "form" else bookings
        keys = ["name", "email", "message"] if scenario["workflow"] == "form" else ["name", "email", "slot"]
        output["required_fields_total"] = len(keys)
        output["required_fields_correct"] = sum((record or {}).get(key) == expected[key] for key in keys)
        count = store.count("drafts" if scenario["workflow"] == "form" else "bookings", state.id)
        output["task_success"] = (output["required_fields_correct"] == len(keys) and count == 1
                                   and (scenario["workflow"] == "form" or record.get("simulated") is True))
    output["scenario_pass"] = output["task_success"]
    return output


def summarize(rows):
    latencies = sorted(row["latency_seconds"] for row in rows)
    achievable = [r for r in rows if r["achievable"]]
    extraction = [r["extraction_accuracy"] for r in rows if r["extraction_accuracy"] is not None]
    field_total = sum(r["required_fields_total"] for r in rows)
    costs = [r["estimated_cost_usd"] for r in rows]
    return {
        "measured_runs": len(rows), "achievable_runs": len(achievable),
        "task_success_rate": sum(r["task_success"] for r in achievable) / len(achievable) if achievable else None,
        "completion_rate_all_scenarios": sum(r["task_success"] for r in rows) / len(rows),
        "scenario_pass_rate": sum(r["scenario_pass"] for r in rows) / len(rows),
        "structured_extraction_accuracy": statistics.mean(extraction) if extraction else None,
        "required_field_completion_accuracy": sum(r["required_fields_correct"] for r in rows) / field_total if field_total else None,
        "median_latency_seconds": statistics.median(latencies),
        "p95_latency_seconds": latencies[math.ceil(.95 * len(latencies)) - 1],
        "median_browser_actions": statistics.median(r["actions"] for r in rows),
        "total_browser_actions": sum(r["actions"] for r in rows),
        "failure_categories": dict(Counter(r["failure_category"] for r in rows if r["failure_category"])),
        "usage": {key: sum(r["usage"][key] for r in rows) for key in ["input_tokens", "cached_tokens", "output_tokens"]},
        "estimated_cost_usd": sum(costs) if all(cost is not None for cost in costs) else None,
        "cost_coverage": sum(cost is not None for cost in costs) / len(costs),
    }


async def evaluate_suite(settings, store, mode, split, repeats=1):
    async with httpx.AsyncClient(trust_env=False) as client:
        health = await client.get(settings.base_url + "/api/health", timeout=5)
        health.raise_for_status()
    scenarios = load_scenarios(split)
    rows = []
    report_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + mode + "-" + split + "-" + secrets.token_hex(3)
    metadata = {"planner_mode": mode, "model": settings.model if mode == "llm" else None,
        "provider": settings.provider if mode == "llm" else None,
        "measured_at": datetime.now(timezone.utc).isoformat(), "split": split,
        "repeats": repeats, "task_ids": [s["id"] for s in scenarios],
        "task_set_sha256": hashlib.sha256(SCENARIOS.read_bytes()).hexdigest(),
        "source_sha256": source_hash(), "python": platform.python_version(),
        "platform": platform.platform(), "browser": None,
        "dependencies": {p: version(p) for p in ["playwright", "langgraph", "fastapi", "openai", "pydantic"]},
        "configuration": {"headless": settings.headless, "base_url": settings.base_url,
            "action_timeout_ms": settings.action_timeout_ms, "max_retries": settings.max_retries,
            "default_max_actions": 25, "default_max_seconds": 90,
            "screenshots": "initial and after every action, masked", "pricing": settings.pricing() if mode == "llm" else None},
        "held_out_policy": "Separate task file entries; not used by planner. Design-time holdout, not a blind external benchmark."}
    for repetition in range(repeats):
        for scenario in scenarios:
            request = RunRequest(task=scenario["task"], workflow=scenario["workflow"], mode=mode,
                variant=scenario.get("variant", "normal"), max_actions=scenario.get("max_actions", 25))
            run_id = secrets.token_hex(12)
            store.create_demo(run_id, request.workflow, request.variant)
            runner = Runner(TaskState(id=run_id, request=request), settings, store)
            started = time.monotonic()
            state = await runner.run()
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            elapsed = time.monotonic() - started
            if runner.browser and runner.browser.browser:
                metadata["browser"] = runner.browser.browser.version
            scores = grade(scenario, state, store, settings.base_url)
            row = {"scenario": scenario["id"], "repeat": repetition + 1, "run_id": run_id,
                "achievable": scenario["achievable"], "status": state.status,
                "failure_category": (state.result or {}).get("failure_category"),
                "latency_seconds": round(elapsed, 4), "actions": state.actions,
                "usage": state.usage, "estimated_cost_usd": (state.result or {}).get("estimated_cost_usd"),
                "blocked_request_count": len(runner.browser.blocked_requests) if runner.browser else 0,
                **scores}
            rows.append(row)
            print(f"{mode}/{split} {scenario['id']}: {state.status}, independent={'pass' if scores['scenario_pass'] else 'FAIL'}", flush=True)
    report = {"id": report_id, "metadata": metadata, "summary": summarize(rows), "rows": rows}
    path = settings.runtime_dir / "evaluations" / (report_id + ".json")
    path.write_text(json.dumps(redact(report), indent=2), encoding="utf-8")
    return report
