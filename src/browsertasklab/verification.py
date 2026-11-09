"""Runtime verification uses fresh visible page evidence, never a planner success claim."""
from urllib.parse import parse_qs, urlsplit


async def verify_visible(state, browser):
    obs, intent = state.observation, state.intent
    if not obs or not intent or intent.unsupported_reason or obs.errors:
        return None
    if intent.workflow == "catalog":
        if state.extraction is None or state.extracted_url != obs.url:
            return None
        params = parse_qs(urlsplit(obs.url).query, keep_blank_values=True)
        # Require an actual GET search, including explicit blank filters.
        if not {"q", "category", "max_price"}.issubset(params):
            return None
        price = params["max_price"][0]
        try:
            same_price = (float(price) if price else None) == intent.max_price
        except ValueError:
            same_price = False
        if (params["q"][0] != (intent.query or "") or params["category"][0] != (intent.category or "All") or not same_price):
            return None
        visible = await browser.extract()
        if visible != state.extraction:
            return None
        return {"completed": "Catalog searched and visible rows extracted",
                "evidence": {"url": obs.url, "visible_row_count": len(visible), "filters": params},
                "data": visible, "outcome": "results" if visible else "empty_results"}
    result = obs.result
    kind = "draft" if intent.workflow == "form" else "booking"
    if not result or result.get("kind") != kind:
        return None
    expected = {"name": intent.name, "email": intent.email}
    if kind == "draft":
        expected["message"] = intent.message
    else:
        expected["slot"] = intent.slot
        if result.get("simulated") != "True":
            return None
    if any(value is None or result.get(key) != value for key, value in expected.items()):
        return None
    if not result.get("id", "").startswith("DRAFT-" if kind == "draft" else "SIM-"):
        return None
    return {"completed": "Contact draft prepared for review" if kind == "draft" else "SIMULATED appointment booked",
            "evidence": {"url": obs.url, "visible_receipt": result}, "data": result,
            "outcome": "draft_ready" if kind == "draft" else "simulated_booking"}


def unresolved_category(state):
    if state.intent and state.intent.unsupported_reason:
        return "missing_details"
    if state.observation and state.observation.errors:
        return "invalid_fields"
    if state.intent and state.intent.workflow == "appointments":
        options = next((c.options for c in state.observation.controls if c.target.name == "Appointment slot"), [])
        if state.intent.slot not in options:
            return "unavailable_slot"
    return "verification_failed"
