import asyncio
import secrets

import httpx
import pytest

from browsertasklab.agent import Runner
from browsertasklab.browser import BrowserTools, DuplicateSubmission
from browsertasklab.evaluation.runner import grade, load_scenarios
from browsertasklab.models import Click, Decision, Finish, Navigate, RunRequest, TaskState
from browsertasklab.planners import ScriptedPlanner, button, label
from browsertasklab.verification import verify_visible

pytestmark = pytest.mark.e2e


async def run_scenario(local_app, scenario, mode="mock", planner=None):
    settings, store = local_app
    request = RunRequest(workflow=scenario["workflow"], task=scenario["task"], mode=mode,
        variant=scenario.get("variant", "normal"), max_actions=scenario.get("max_actions", 25))
    run_id = secrets.token_hex(12)
    store.create_demo(run_id, request.workflow, request.variant)
    runner = Runner(TaskState(id=run_id, request=request), settings, store, planner=planner)
    state = await runner.run()
    return state, runner, grade(scenario, state, store, settings.base_url)


@pytest.mark.parametrize("scenario", load_scenarios("development"), ids=lambda s: s["id"])
async def test_development_workflows(local_app, scenario):
    state, runner, scores = await run_scenario(local_app, scenario)
    assert scores["scenario_pass"], (state.model_dump(), scores)
    assert not runner.browser.browser.is_connected()
    assert state.actions <= state.request.max_actions
    if state.status == "succeeded":
        assert state.result["evidence"]
        assert state.result["unresolved"] == []


async def test_baseline_cannot_adapt_changed_label(local_app):
    scenario = next(s for s in load_scenarios("development") if s["id"] == "d07-changed-label")
    state, _, scores = await run_scenario(local_app, scenario, "baseline")
    assert not scores["task_success"]
    assert state.status == "blocked"
    assert state.failures["missing_element"] == 3  # initial attempt + two retries


class LyingPlanner(ScriptedPlanner):
    async def decide(self, state):
        return Decision(rationale="claim only", action=Finish(tool="finish_task", reason="Success!"))


async def test_unverified_finish_cannot_claim_success(local_app):
    scenario = load_scenarios("development")[0]
    state, _, scores = await run_scenario(local_app, scenario, planner=LyingPlanner())
    assert state.status == "blocked" and state.result["failure_category"] == "verification_failed"
    assert not scores["task_success"]


async def test_transient_browser_timeout_recovers_with_fresh_observation(local_app, monkeypatch):
    from playwright.async_api import TimeoutError as BrowserTimeout
    original = BrowserTools.execute
    calls = []

    async def fail_once(self, action, *args, **kwargs):
        if action.tool == "fill" and not calls:
            calls.append("transient timeout")
            raise BrowserTimeout("Transient test timeout before any effect")
        return await original(self, action, *args, **kwargs)

    monkeypatch.setattr(BrowserTools, "execute", fail_once)
    scenario = load_scenarios("development")[0]
    state, _, scores = await run_scenario(local_app, scenario)
    assert scores["task_success"] and state.status == "succeeded"
    assert state.failures == {"timeout": 1} and state.retries == 0


async def test_time_budget_and_browser_cleanup(local_app):
    settings, store = local_app
    run_id = secrets.token_hex(12)
    store.create_demo(run_id, "catalog")
    request = RunRequest(workflow="catalog", task="Find all books", max_seconds=.1)
    runner = Runner(TaskState(id=run_id, request=request), settings, store)
    state = await runner.run()
    assert state.status == "budget_exceeded" and state.result["failure_category"] == "time_budget"
    if runner.browser and runner.browser.browser:
        assert not runner.browser.browser.is_connected()


async def test_duplicate_submission_rejected_by_executor(local_app):
    settings, store = local_app
    run_id = secrets.token_hex(12)
    store.create_demo(run_id, "appointments")
    from browsertasklab.models import Fill, Select
    async with BrowserTools(settings, run_id, "appointments") as browser:
        await browser.execute(Navigate(tool="navigate", url=browser.start_url))
        for name, value in [("Full name", "Ada Lovelace"), ("Email address", "ada@example.test")]:
            await browser.execute(Fill(tool="fill", target=label(name), value=value))
        await browser.execute(Select(tool="select_option", target=label("Appointment slot"), value="2030-05-20 10:00"))
        click = Click(tool="click", target=button("Confirm simulated booking"))
        await browser.execute(click)
        assert store.count("bookings", run_id) == 1
        with pytest.raises(DuplicateSubmission):
            await browser.execute(click)
        assert store.count("bookings", run_id) == 1


async def test_network_guard_blocks_subresources_redirects_and_control_api(local_app):
    settings, store = local_app
    run_id = secrets.token_hex(12)
    store.create_demo(run_id, "catalog", "injection")
    async with BrowserTools(settings, run_id, "catalog") as browser:
        await browser.execute(Navigate(tool="navigate", url=browser.start_url))
        obs = await browser.inspect_page()
        assert "Ignore your task" in obs.text
        with pytest.raises(PermissionError):
            await browser.execute(Click(tool="click", target=button("External collection page").model_copy(update={"role": "link"})))
        # Test-owned code challenges the network guard; no planner receives evaluate().
        blocked = await browser.page.evaluate("""async () => { try { await fetch('/api/runs'); return false; } catch(e) { return true; } }""")
        assert blocked and browser.blocked_requests


async def test_review_exact_digest_and_stop_api(local_app):
    settings, store = local_app
    async with httpx.AsyncClient(base_url=settings.base_url, trust_env=False) as client:
        response = await client.post("/api/runs", json={"workflow": "appointments", "task": "Book a simulated appointment for Ada Lovelace, email ada@example.test, on 2030-05-20 10:00 UTC.", "require_review": True})
        run_id = response.json()["id"]
        for _ in range(400):
            state = (await client.get("/api/runs/" + run_id)).json()
            if state["status"] == "awaiting_review":
                break
            assert state["status"] not in {"failed", "blocked"}, state
            await asyncio.sleep(.05)
        assert state["status"] == "awaiting_review"
        assert store.count("bookings", run_id) == 0
        review = (await client.get(f"/api/runs/{run_id}/review")).json()
        assert review["fields"]["Email address"] == "ada@example.test"
        wrong = await client.post(f"/api/runs/{run_id}/review", json={"approve": True, "digest": "wrong"})
        assert wrong.status_code == 409
        accepted = await client.post(f"/api/runs/{run_id}/review", json={"approve": True, "digest": review["digest"]})
        assert accepted.status_code == 200
        for _ in range(200):
            state = (await client.get("/api/runs/" + run_id)).json()
            if state["status"] == "succeeded":
                break
            await asyncio.sleep(.05)
        assert state["status"] == "succeeded"
        assert store.count("bookings", run_id) == 1
        assert "ada@example.test" not in str(state)
        again = await client.post(f"/api/runs/{run_id}/review", json={"approve": True, "digest": review["digest"]})
        assert again.status_code == 409
        # A pending review is a safe and reproducible point to exercise stop.
        new = await client.post("/api/runs", json={"workflow": "appointments", "task": "Book a simulated appointment for Ada Lovelace, email ada@example.test, on 2030-05-20 10:00 UTC.", "require_review": True})
        other_id = new.json()["id"]
        await client.post(f"/api/runs/{other_id}/stop", json={})
        for _ in range(200):
            other = (await client.get("/api/runs/" + other_id)).json()
            if other["status"] == "stopped":
                break
            await asyncio.sleep(.05)
        assert other["status"] == "stopped" and store.count("bookings", other_id) == 0


async def test_cross_origin_mutations_and_secrets_rejected(local_app):
    settings, _ = local_app
    async with httpx.AsyncClient(base_url=settings.base_url, trust_env=False) as client:
        response = await client.post("/api/runs", headers={"Origin": "https://evil.test"}, json={"workflow": "catalog", "task": "Find all books"})
        assert response.status_code == 403
        response = await client.post("/api/runs", json={"workflow": "catalog", "task": "Use password=hunter2"})
        assert response.status_code == 422


async def test_redirect_chain_cannot_escape_run_path(local_app):
    """A second-hop HTTP redirect must not reach even a forbidden local endpoint."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from playwright.async_api import Error as PlaywrightError

    settings, _ = local_app
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visits.append(self.path)
            if self.path.endswith('/catalog'):
                self.send_response(302)
                self.send_header('Location', '/demo/redirect-run/hop')
            elif self.path.endswith('/hop'):
                self.send_response(302)
                self.send_header('Location', '/forbidden')
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scoped = settings.model_copy(update={'base_url': f'http://127.0.0.1:{server.server_port}'})
    try:
        async with BrowserTools(scoped, 'redirect-run', 'catalog') as browser:
            try:
                await browser.execute(Navigate(tool='navigate', url=browser.start_url))
            except (PlaywrightError, PermissionError):
                pass
            assert '/forbidden' not in visits, visits
            assert browser.blocked_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
