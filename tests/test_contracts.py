from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from browsertasklab.config import Settings
from browsertasklab.models import Action, Decision, Intent, RunRequest, Target
from browsertasklab.planners import LLMPlanner, SYSTEM, parse_scripted
from browsertasklab.security import DomainPolicy, redact, reject_secrets
from browsertasklab.store import Store


@pytest.mark.parametrize("payload", [
    {"tool": "shell", "cmd": "whoami"},
    {"tool": "navigate", "url": "http://127.0.0.1", "code": "evil()"},
    {"tool": "fill", "target": {"by": "label", "name": "", "role": None}, "value": "x"},
    {"tool": "click", "target": {"by": "role", "name": "Submit", "role": None}},
    {"tool": "scroll", "direction": "left"},
    {"tool": "extract_structured_data", "schema_name": "secret"},
])
def test_invalid_tool_arguments(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(Action).validate_python(payload)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8000/demo/abc/catalog", "http://127.0.0.1:8001/demo/abc/catalog",
    "http://127.0.0.1.evil.test:8000/demo/abc/catalog", "http://evil.test/demo/abc/catalog",
    "http://user@127.0.0.1:8000/demo/abc/catalog", "javascript:alert(1)",
    "http://127.0.0.1:8000/api/runs", "http://127.0.0.1:8000/demo/other/catalog",
    "http://127.0.0.1:8000/demo/abc/%252e%252e/api/runs", "file:///etc/passwd",
])
def test_exact_origin_and_path_restrictions(url):
    with pytest.raises(PermissionError):
        DomainPolicy("http://127.0.0.1:8000", "abc").check(url)


def test_own_demo_allowed_static_resource_only():
    policy = DomainPolicy("http://127.0.0.1:8000", "abc")
    assert policy.check("http://127.0.0.1:8000/demo/abc/catalog?q=Python")
    assert policy.check("http://127.0.0.1:8000/static/demo.js", resource=True)
    with pytest.raises(PermissionError):
        policy.check("http://127.0.0.1:8000/static/app.js", resource=True)


def test_real_origin_configuration_rejected():
    with pytest.raises(ValueError):
        DomainPolicy("https://example.com", "abc")


def test_trace_redaction_nested_and_secret_rejection():
    data = {"password": "hidden", "nested": [{"text": "mail ada@example.test sk-abcdefghijklmnop password=hunter2 4111 1111 1111 1111"}], "authorization": "Bearer abcdef"}
    result = str(redact(data))
    for secret in ["hidden", "ada@example.test", "abcdefghijklmnop", "hunter2", "4111", "abcdef"]:
        assert secret not in result
    assert "[REDACTED]" in result
    with pytest.raises(ValueError):
        reject_secrets("Use my password=secret")
    reject_secrets("Book for Ada on 2030-05-20 10:00, email ada@example.test")


def test_sqlite_idempotency_and_conflicting_submission(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    first = {"name": "Ada", "id": "SIM-test"}
    assert store.commit_once("bookings", "test", first) == first
    assert store.commit_once("bookings", "test", first) == first
    assert store.count("bookings", "test") == 1
    with pytest.raises(ValueError):
        store.commit_once("bookings", "test", {"name": "Different"})


def test_scripted_task_grammar_does_not_invent_missing_details():
    result = parse_scripted('Search for Python books under $40.', "catalog")
    assert (result.query, result.category, result.max_price) == ("Python", "Books", 40)
    result = parse_scripted("Prepare a draft for Ada Lovelace", "form")
    assert result.unsupported_reason
    assert result.email is None and result.message is None


def test_model_pricing_requires_matching_model_and_provenance():
    settings = Settings(price_input=.75, price_cached=.075, price_output=4.5)
    assert settings.pricing() is None
    settings.price_model = settings.model
    settings.price_verified_on = "2026-11-09"
    settings.price_source = "https://developers.openai.com/api/docs/models/gpt-5.4-mini"
    assert settings.pricing()["output"] == 4.5
    settings.model = "different"
    assert settings.pricing() is None


async def test_real_planner_sdk_contract_with_stubbed_response():
    """This is a mocked API unit test, never counted as a model evaluation."""
    intent = Intent.empty("catalog", query="Python", category="Books", max_price=40)
    response = SimpleNamespace(status="completed", output_parsed=intent,
        usage=SimpleNamespace(input_tokens=123, output_tokens=17, input_tokens_details=SimpleNamespace(cached_tokens=20)))
    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)), close=AsyncMock())
    planner = LLMPlanner(Settings(), client=client)
    got = await planner.interpret(RunRequest(workflow="catalog", task="Find Python books"))
    assert got == intent
    kwargs = client.responses.parse.call_args.kwargs
    assert kwargs["text_format"] == Intent and kwargs["store"] is False
    assert "UNTRUSTED DATA" in kwargs["input"][0]["content"]
    assert planner.usage == {"input_tokens": 123, "output_tokens": 17, "cached_tokens": 20}
    response.status = "incomplete"
    with pytest.raises(ValueError, match="incomplete"):
        await planner.interpret(RunRequest(workflow="catalog", task="Find Python books"))
    await planner.close()
