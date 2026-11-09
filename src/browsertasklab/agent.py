"""Observe → plan → act → verify, with deterministic stopping and cleanup."""
import asyncio
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from playwright.async_api import TimeoutError as BrowserTimeout
from pydantic import ValidationError

from .browser import BrowserTools, DuplicateSubmission, ReviewRequired
from .models import Navigate, TaskState, TERMINAL
from .planners import make_planner
from .security import redact
from .verification import unresolved_category, verify_visible


class GraphState(TypedDict):
    state: TaskState


class Runner:
    def __init__(self, state, settings, store, planner=None):
        self.state, self.settings, self.store = state, settings, store
        self.planner = planner
        self.browser = None
        self.stop_event = asyncio.Event()
        self.review_event = asyncio.Event()
        self.review_approved = False
        self.started = time.monotonic()
        self.decision = None

    def publish(self, phase):
        self.state.phase = phase
        self.state.elapsed_seconds = round(time.monotonic() - self.started, 4)
        if self.planner and self.planner.usage:
            self.state.usage = self.planner.usage.copy()
        self.store.save_run(self.state)

    def finish(self, status, category=None, evidence=None, unresolved=None):
        self.state.status = status
        if self.planner and self.planner.usage:
            self.state.usage = self.planner.usage.copy()
        usage = self.state.usage
        pricing = self.settings.pricing() if self.state.request.mode == "llm" else None
        cost = None
        if pricing and any(usage.values()):
            cost = ((usage["input_tokens"] - usage["cached_tokens"]) * pricing["input"] +
                    usage["cached_tokens"] * pricing["cached"] + usage["output_tokens"] * pricing["output"]) / 1_000_000
        self.state.result = {"status": status, "failure_category": category,
            "planner_mode": self.state.request.mode, "model": self.state.model,
            "simulated": True, "completed": evidence["completed"] if evidence else "No verified completion",
            "unresolved": unresolved or ([] if evidence else [self.state.last_error or "Task outcome could not be verified"]),
            "evidence": evidence["evidence"] if evidence else None,
            "data": evidence["data"] if evidence else None,
            "outcome": evidence["outcome"] if evidence else None,
            "partial_progress": None if evidence else {
                "filled_field_labels": [key for key, value in (self.state.observation.fields if self.state.observation else {}).items() if value],
                "last_observed_url": self.state.observation.url if self.state.observation else None},
            "usage": usage, "estimated_cost_usd": cost, "pricing": pricing,
            "actions": self.state.actions}
        self.state.pending_review = None
        self.publish("finished")

    def check_limits(self):
        if self.stop_event.is_set():
            self.finish("stopped", "user_stopped", unresolved=["Stopped by the user; no further actions will run"])
        elif self.state.actions >= self.state.request.max_actions:
            self.finish("budget_exceeded", "action_budget")
        return self.state.status in TERMINAL

    async def observe(self, data):
        if not self.check_limits():
            self.publish("observe")
            self.state.observation = await self.browser.inspect_page()
            if not self.state.screenshots:
                self.state.screenshots.append(await self.browser.screenshot(0))
            self.publish("observed")
        return {"state": self.state}

    async def plan(self, data):
        if self.check_limits():
            return {"state": self.state}
        self.publish("plan")
        self.decision = await self.planner.decide(self.state)
        self.publish("planned")
        return {"state": self.state}

    def record_failure(self, error):
        if isinstance(error, PermissionError):
            category = "domain_restriction"
        elif isinstance(error, LookupError):
            category = "missing_element"
        elif isinstance(error, BrowserTimeout):
            category = "timeout"
        elif isinstance(error, DuplicateSubmission):
            category = "duplicate_submission"
        elif isinstance(error, (ValidationError, ValueError)):
            category = "validation_error"
        else:
            category = "browser_error"
        self.state.last_error = f"{type(error).__name__}: {str(error)[:500]}"
        self.state.failures[category] = self.state.failures.get(category, 0) + 1
        self.state.retries += 1
        return category

    async def act(self, data):
        if self.check_limits():
            return {"state": self.state}
        action = self.decision.action
        self.publish("act")
        category = None
        try:
            try:
                output = await self.browser.execute(action, self.state.actions + 1)
            except ReviewRequired as review:
                self.state.status = "awaiting_review"
                self.state.pending_review = review.proposal
                self.publish("awaiting_review")
                await self.review_event.wait()
                self.review_event.clear()
                if not self.review_approved or self.stop_event.is_set():
                    self.finish("stopped", "review_declined", unresolved=["The proposed simulated booking was not authorized"])
                    return {"state": self.state}
                self.browser.authorized_digest = review.proposal["digest"]
                self.state.pending_review = None
                self.state.status = "running"
                output = await self.browser.execute(action, self.state.actions + 1)
            self.state.last_error = None
            self.state.retries = 0
            if action.tool == "extract_structured_data":
                self.state.extraction = output
                self.state.extracted_url = self.browser.page.url
        except (PermissionError, LookupError, BrowserTimeout, DuplicateSubmission, ValidationError, ValueError) as error:
            category = self.record_failure(error)
        self.state.actions += 1
        self.state.history.append(redact({"step": self.state.actions, "phase": "act",
            "action": action.model_dump(), "rationale": self.decision.rationale,
            "ok": category is None, "failure_category": category, "error": self.state.last_error}))
        if category == "domain_restriction":
            self.finish("blocked", category)
        self.publish("acted")
        return {"state": self.state}

    async def verify(self, data):
        if self.state.status in TERMINAL:
            return {"state": self.state}
        self.publish("verify")
        self.state.observation = await self.browser.inspect_page()  # mandatory post-action observation
        if self.planner.usage:
            self.state.usage = self.planner.usage.copy()
        evidence = await verify_visible(self.state, self.browser)
        self.state.screenshots.append(await self.browser.screenshot(self.state.actions))
        if evidence:
            self.finish("succeeded", evidence=evidence)
        elif self.state.retries > self.settings.max_retries:
            category = next(reversed(self.state.failures), "retry_limit")
            self.finish("blocked", category, unresolved=[self.state.last_error or "Retry limit reached"])
        elif self.decision.action.tool == "finish_task":
            self.finish("blocked", unresolved_category(self.state), unresolved=[self.decision.action.reason])
        else:
            self.check_limits()
            self.publish("verified")
        return {"state": self.state}

    def graph(self):
        graph = StateGraph(GraphState)
        for name in ["observe", "plan", "act", "verify"]:
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "observe")
        for source, target in [("observe", "plan"), ("plan", "act"), ("act", "verify"), ("verify", "observe")]:
            graph.add_conditional_edges(source, lambda d, target=target: END if d["state"].status in TERMINAL else target)
        return graph.compile()

    async def run(self):
        self.started = time.monotonic()
        self.state.status = "running"
        self.publish("starting")
        try:
            async with asyncio.timeout(self.state.request.max_seconds):
                self.planner = self.planner or make_planner(self.state.request.mode, self.settings)
                self.state.model = self.planner.model
                async with BrowserTools(self.settings, self.state.id, self.state.request.workflow,
                                        self.state.request.require_review) as browser:
                    self.browser = browser
                    await browser.execute(Navigate(tool="navigate", url=browser.start_url))
                    self.state.actions = 1
                    self.state.history.append({"step": 1, "phase": "bootstrap", "action": {"tool": "navigate", "url": browser.start_url}, "ok": True})
                    self.state.observation = await browser.inspect_page()
                    self.publish("interpreting_task")
                    self.state.intent = await self.planner.interpret(self.state.request)
                    await self.graph().ainvoke({"state": self.state}, {"recursion_limit": 4 * self.state.request.max_actions + 10})
        except TimeoutError:
            self.finish("budget_exceeded", "time_budget", unresolved=["The wall-clock time budget expired (including review time)"])
        except asyncio.CancelledError:
            self.finish("stopped", "user_stopped", unresolved=["Run cancelled; browser closed"])
        except Exception as error:
            self.state.last_error = f"{type(error).__name__}: {str(error)[:500]}"
            category = "planner_error" if self.state.phase in {"starting", "plan", "interpreting_task"} else "execution_error"
            self.finish("failed", category)
        finally:
            if self.planner:
                if self.planner.usage:
                    self.state.usage = self.planner.usage.copy()
                await self.planner.close()
            self.publish("finished")
        return self.state
