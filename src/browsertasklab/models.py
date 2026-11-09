from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Workflow = Literal["catalog", "form", "appointments"]
Mode = Literal["mock", "baseline", "llm"]
Status = Literal["queued", "running", "awaiting_review", "succeeded", "blocked",
                 "budget_exceeded", "stopped", "failed"]
TERMINAL = {"succeeded", "blocked", "budget_exceeded", "stopped", "failed"}


class RunRequest(StrictModel):
    task: str = Field(min_length=3, max_length=2000)
    workflow: Workflow
    mode: Mode = "mock"
    max_actions: int = Field(default=25, ge=1, le=60)
    max_seconds: float = Field(default=90, ge=0.1, le=180)
    require_review: bool = False
    variant: Literal["normal", "injection", "changed", "missing", "unavailable"] = "normal"


class Intent(StrictModel):
    """A task contract. Nullable fields are required in the model schema."""
    workflow: Workflow
    query: str | None
    category: Literal["All", "Books", "Electronics", "Home"] | None
    max_price: float | None = Field(ge=0, le=1000000)
    name: str | None
    email: str | None
    message: str | None
    slot: str | None
    unsupported_reason: str | None

    @classmethod
    def empty(cls, workflow: Workflow, **kw):
        data = dict(workflow=workflow, query=None, category=None, max_price=None,
                    name=None, email=None, message=None, slot=None, unsupported_reason=None)
        return cls(**(data | kw))


class Target(StrictModel):
    by: Literal["role", "label", "test_id"]
    name: str = Field(min_length=1, max_length=160)
    role: Literal["button", "link", "textbox", "combobox"] | None

    @model_validator(mode="after")
    def role_contract(self):
        if (self.by == "role") != (self.role is not None):
            raise ValueError("Role targets require a role; labels and test IDs use role=null")
        return self


class Navigate(StrictModel):
    tool: Literal["navigate"]
    url: str = Field(min_length=1, max_length=2048)


class Inspect(StrictModel):
    tool: Literal["inspect_page"]


class Click(StrictModel):
    tool: Literal["click"]
    target: Target


class Fill(StrictModel):
    tool: Literal["fill"]
    target: Target
    value: str = Field(max_length=1000)


class Select(StrictModel):
    tool: Literal["select_option"]
    target: Target
    value: str = Field(max_length=100)


class Scroll(StrictModel):
    tool: Literal["scroll"]
    direction: Literal["up", "down"]


class Extract(StrictModel):
    tool: Literal["extract_structured_data"]
    schema_name: Literal["catalog"]


class Screenshot(StrictModel):
    tool: Literal["capture_screenshot"]


class Finish(StrictModel):
    tool: Literal["finish_task"]
    reason: str = Field(max_length=500)


# A plain union emits anyOf (supported by the Responses structured-output subset).
# Literal tool names still validate each variant; no arbitrary dictionaries reach execution.
Action = Navigate | Inspect | Click | Fill | Select | Scroll | Extract | Screenshot | Finish


class Decision(StrictModel):
    rationale: str = Field(max_length=500)
    action: Action


class Control(StrictModel):
    target: Target
    value: str
    options: list[str] = []
    disabled: bool = False


class Observation(StrictModel):
    url: str
    title: str
    text: str
    controls: list[Control]
    fields: dict[str, str]
    errors: list[str]
    result: dict | None = None


class TaskState(StrictModel):
    id: str
    request: RunRequest
    status: Status = "queued"
    phase: str = "queued"
    intent: Intent | None = None
    observation: Observation | None = None
    history: list[dict] = []
    actions: int = 0
    retries: int = 0
    failures: dict[str, int] = {}
    extraction: list[dict] | None = None
    extracted_url: str | None = None
    last_error: str | None = None
    pending_review: dict | None = None
    result: dict | None = None
    usage: dict[str, int] = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    model: str | None = None
    elapsed_seconds: float = 0
    screenshots: list[str] = []


class Contact(StrictModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=3, max_length=160)
    message: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def email_valid(self):
        import re
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise ValueError("Use a valid email address")
        return self


class Booking(StrictModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=3, max_length=160)
    slot: str

    @model_validator(mode="after")
    def email_valid(self):
        Contact(name=self.name, email=self.email, message="booking")
        return self
