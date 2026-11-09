"""Trust boundaries live in code, never in the model's opinion of a page."""
import re
from urllib.parse import unquote, urlsplit

SENSITIVE_KEY = re.compile(r"password|passwd|secret|api.?key|authorization|cookie|payment|card.number|cvv", re.I)
SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]+)\b", re.I)
ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|secret|api[_ -]?key|token|cvv)\s*[:=]\s*[^\s;,]+")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE_KEY.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Catch encoded secrets in error URLs as well as plain text.
        if "%40" in value.lower() or "%3d" in value.lower() or "%3a" in value.lower():
            value = unquote(value)
        value = SECRET.sub("[REDACTED]", value)
        value = ASSIGNMENT.sub(lambda m: m[1] + "=[REDACTED]", value)
        return CARD.sub("[REDACTED]", EMAIL.sub("[EMAIL]", value))
    return value


def reject_secrets(task: str):
    if SECRET.search(task) or ASSIGNMENT.search(task) or CARD.search(task):
        raise ValueError("Use synthetic demo details only; passwords, API keys and payment data are not accepted.")


class DomainPolicy:
    """Exact scheme/host/port AND per-run path, including subresource requests."""
    def __init__(self, base_url: str, run_id: str):
        self.base = urlsplit(base_url)
        if self.base.scheme != "http" or self.base.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MVP supports an HTTP loopback demo origin only")
        self.prefix = f"/demo/{run_id}/"

    def check(self, url: str, *, resource=False):
        parsed = urlsplit(url)
        try:
            same_origin = ((parsed.scheme, parsed.hostname, parsed.port or 80) ==
                           (self.base.scheme, self.base.hostname, self.base.port or 80))
        except ValueError:
            same_origin = False
        path = unquote(unquote(parsed.path))
        if (not same_origin or parsed.username or parsed.password or "\\" in path
                or any(part in {".", ".."} for part in path.split("/"))):
            raise PermissionError("Navigation outside the configured demo origin is blocked")
        allowed = path.startswith(self.prefix)
        if resource:
            allowed |= path in {"/static/demo.css", "/static/demo.js"}
        if not allowed:
            raise PermissionError("Only this run's demo pages are allowed; control APIs are inaccessible")
        return url
