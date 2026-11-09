# Model integration and cost

The default actual-model configuration is OpenAI Responses API with
`gpt-5.4-mini-2026-03-17`. The dated snapshot makes the configuration explicit.
This is a reasonable candidate for a small structured planning task, chosen for its
documented structured-output support and lower token prices than the full GPT-5.4
model. It has **not** been validated for task quality in this repository yet.

The official GPT-5 Mini page marks its older snapshot deprecated, so it is not the
default. The model setting can be changed without changing browser execution.
Account access, provider availability, and lifecycle must be checked before a paid run.

## Actual implementation

`AsyncOpenAI.responses.parse(model=..., text_format=<Pydantic model>, store=False)`
first interprets the user task into `Intent`, then proposes one `Decision` per step.
The union of typed actions emits `anyOf`; it avoids discriminated `oneOf`, which is
outside the documented structured-output schema subset. Refusals, incomplete output,
schema errors, and provider errors fail visibly. There is no silent fallback to mock.
The SDK has a 30-second request timeout, automatic SDK retries are disabled, output is
bounded to 1,800 tokens per call, and the whole run has its own time/action budgets.
The default model's reasoning setting is left at its documented default (`none`).

`BTL_PROVIDER=openai-compatible` plus `BTL_MODEL_BASE_URL` can target another provider
that genuinely implements Responses API parsing with the same structured-output
contract. Chat-Completions-only endpoints do not qualify. This adapter is configurable,
not a claim that every provider has been tested. Implement the `interpret`, `decide`,
`close`, `usage`, and `model` interface for another API dialect.

`OPENAI_API_KEY` is read from the environment or ignored `.env`; it is never exposed
in the UI or passed to browser pages. Do not put keys in a task. Mock and baseline
modes make no model requests and require no key.

## Pricing assumptions

Official prices checked on **2026-11-09**, standard text tokens, USD per million:

| Exact configured model | Uncached input | Cached input | Output |
| --- | ---: | ---: | ---: |
| gpt-5.4-mini-2026-03-17 | $0.75 | $0.075 | $4.50 |

These exclude tax, regional processing uplifts, provider-specific charges, and
future price changes. No hosted computer-use, search, or other paid tools are used.
Playwright runs locally. Recheck the source before enabling the commented pricing
variables in `.env.example`.

Cost is null unless actual API token usage exists and an explicit pricing record
names the exact configured model, source URL, and verification date. The estimate is

```text
((input_tokens - cached_tokens) × input_price
 + cached_tokens × cached_price
 + output_tokens × output_price) / 1,000,000
```

For illustration only, 10,000 uncached input tokens plus 1,000 output tokens would
cost $0.012 at these prices. That is arithmetic, not measured usage or a per-task
forecast. Repeated page observations and model reasoning can change consumption.
Usage returned by successfully received responses is accumulated; an interrupted or
failed request may be billed without returning usage. Reports cannot establish the
full provider bill in that case. Compare reports against the provider's usage ledger.

No actual model calls were made during initial development because no API key was
available. Mocked SDK unit tests validate the adapter contract only. They do not
validate the remote schema acceptance, model quality, latency, or actual billing.

## Primary sources

- [OpenAI structured outputs and Python parsing](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [GPT-5.4 Mini capabilities, snapshots, reasoning defaults and pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
- [OpenAI Python SDK reference](https://developers.openai.com/api/reference/python)
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [Playwright semantic locators](https://playwright.dev/python/docs/locators)
- [Playwright installation](https://playwright.dev/python/docs/intro)
- [Playwright routing and redirect behavior](https://playwright.dev/python/docs/api/class-page#page-route)
- [Playwright fetch without automatic redirects](https://playwright.dev/python/docs/api/class-route#route-fetch)
- [FastAPI server operation](https://fastapi.tiangolo.com/deployment/manually/)

Dependency releases were checked against package-maintainer metadata on PyPI on
2026-11-09 and installed under Python 3.12.14. Direct dependencies are pinned in
`pyproject.toml`; `requirements.lock` captures the tested transitive environment.
