from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browsertasklab.evaluation.runner import grade, load_scenarios, summarize
from browsertasklab.models import Decision, Intent, Observation, RunRequest, TaskState
from browsertasklab.store import Store
from browsertasklab.verification import verify_visible


async def test_verification_requires_matching_visible_receipt():
    state = TaskState(id="abc", request=RunRequest(workflow="appointments", task="Book simulated appointment"))
    state.intent = Intent.empty("appointments", name="Ada", email="ada@example.test", slot="2030-05-20 10:00")
    state.observation = Observation(url="http://127.0.0.1/demo/abc/appointments", title="Demo", text="Success", controls=[], fields={}, errors=[],
        result={"kind": "booking", "name": "Ada", "email": "ada@example.test", "slot": "2030-05-20 09:00", "id": "SIM-abc", "simulated": "True"})
    assert await verify_visible(state, None) is None
    state.observation.result["slot"] = "2030-05-20 10:00"
    assert (await verify_visible(state, None))["outcome"] == "simulated_booking"
    state.observation.errors = ["Validation failed"]
    assert await verify_visible(state, None) is None


def test_evaluator_ignores_agents_own_success_claim(tmp_path):
    scenario = next(s for s in load_scenarios("development") if s["id"] == "d03-valid-draft")
    state = TaskState(id="abc", request=RunRequest(workflow="form", task=scenario["task"]),
                      status="succeeded", result={"completed": "I succeeded!"})
    store = Store(tmp_path / "test.sqlite3")
    assert not grade(scenario, state, store, "http://127.0.0.1:8000")["task_success"]
    store.commit_once("drafts", "abc", scenario["expected"] | {"id": "DRAFT-abc"})
    assert grade(scenario, state, store, "http://127.0.0.1:8000")["task_success"]


def test_scenario_splits_and_metric_definitions():
    dev, held = load_scenarios("development"), load_scenarios("held_out")
    assert len(dev) == len(held) == 10
    assert not {s["id"] for s in dev} & {s["id"] for s in held}
    rows = [{"latency_seconds": n, "achievable": True, "task_success": n < 3,
             "scenario_pass": n < 3, "extraction_accuracy": .5,
             "required_fields_correct": 2, "required_fields_total": 3,
             "actions": 4, "estimated_cost_usd": None, "failure_category": None,
             "usage": {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}} for n in [1, 2, 3, 4]]
    report = summarize(rows)
    assert report["median_latency_seconds"] == 2.5
    assert report["p95_latency_seconds"] == 4
    assert report["task_success_rate"] == .5
    assert report["required_field_completion_accuracy"] == 2 / 3
    assert report["estimated_cost_usd"] is None


def test_structured_output_schema_uses_supported_union_shape():
    schema = Decision.model_json_schema()
    assert "anyOf" in schema["properties"]["action"]
    assert "oneOf" not in schema["properties"]["action"]
