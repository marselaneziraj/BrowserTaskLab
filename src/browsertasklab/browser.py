"""Typed browser execution. The planner cannot supply selectors, JS, or shell code."""
import asyncio
import hashlib
import json
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, async_playwright
from pydantic import TypeAdapter

from .models import Action, Observation, Target
from .security import DomainPolicy, redact


class ReviewRequired(Exception):
    def __init__(self, proposal):
        self.proposal = proposal


class DuplicateSubmission(Exception):
    pass


class BrowserTools:
    def __init__(self, settings, run_id, workflow, require_review=False):
        self.settings = settings
        self.run_id = run_id
        self.workflow = workflow
        self.policy = DomainPolicy(settings.base_url, run_id)
        self.require_review = require_review
        self.attempted_effects = set()
        self.authorized_digest = None
        self.pw = self.browser = self.context = self.page = None
        self.blocked_requests = []

    async def __aenter__(self):
        # Let driver startup settle before cleanup if the budget expires during startup.
        # Cancelling Playwright.start() itself can strand its connection task on Windows.
        startup = asyncio.create_task(async_playwright().start())
        try:
            self.pw = await asyncio.shield(startup)
            self.browser = await self.pw.chromium.launch(headless=self.settings.headless)
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 900}, service_workers="block",
                accept_downloads=False)
            await self.context.route("**/*", self._route)
            await self.context.route_web_socket("**/*", lambda ws: ws.close())
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.settings.action_timeout_ms)
            self.page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
            self.context.on("page", self._new_page)
            return self
        except BaseException:
            if self.pw is None:
                try:
                    self.pw = await startup
                except Exception:
                    pass
            await self.__aexit__(None, None, None)
            raise

    async def _new_page(self, page):
        if page != self.page:
            await page.close()

    async def _route(self, route):
        response = None
        try:
            self.policy.check(route.request.url, resource=not route.request.is_navigation_request())
            # continue_() can follow redirects without invoking the route handler again.
            # Fetch exactly once without redirects, then fulfill the original request.
            # These demos do not need redirects; fail closed for every 3xx Location.
            response = await route.fetch(max_redirects=0, max_retries=0,
                                         timeout=self.settings.action_timeout_ms)
            if 300 <= response.status < 400:
                self.blocked_requests.append(redact(route.request.url + " [HTTP redirect blocked]"))
                await route.abort("blockedbyclient")
            else:
                await route.fulfill(response=response)
        except PermissionError:
            self.blocked_requests.append(redact(route.request.url))
            await route.abort("blockedbyclient")
        except PlaywrightError:
            await route.abort("failed")
        finally:
            if response:
                await response.dispose()

    async def __aexit__(self, *_):
        try:
            if self.context:
                await self.context.close()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            finally:
                if self.pw:
                    await self.pw.stop()

    @property
    def start_url(self):
        return f"{self.settings.base_url.rstrip('/')}/demo/{self.run_id}/{self.workflow}"

    def locator(self, target: Target):
        if target.by == "label":
            return self.page.get_by_label(target.name, exact=True)
        if target.by == "test_id":
            return self.page.get_by_test_id(target.name)
        if not target.role:
            raise ValueError("A role target requires a role")
        return self.page.get_by_role(target.role, name=target.name, exact=True)

    async def inspect_page(self):
        self.policy.check(self.page.url)
        # This fixed, read-only DOM reader is owned by the executor. No model code is evaluated.
        data = await self.page.evaluate("""() => {
          const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
          const controls = []; const fields = {};
          for (const e of document.querySelectorAll('input:not([type=hidden]),textarea,select,button,a')) {
            if (!visible(e)) continue;
            if (e.matches('input,textarea,select')) {
              const labelNode = e.labels?.[0]?.cloneNode(true);
              if (labelNode) labelNode.querySelectorAll('input,textarea,select').forEach(x=>x.remove());
              const label = (labelNode?.textContent || e.getAttribute('aria-label') || '').trim();
              if (!label) continue;
              const value = e.type === 'password' ? '[REDACTED]' : e.value;
              fields[label] = value;
              controls.push({target:{by:'label',name:label,role:null},value,
                options:e.tagName === 'SELECT' ? Array.from(e.options).map(x=>x.value) : [],disabled:e.disabled});
            } else {
              controls.push({target:{by:'role',role:e.tagName === 'A'?'link':'button',
                name:(e.getAttribute('aria-label')||e.textContent).trim()},value:'',options:[],disabled:!!e.disabled});
            }
          }
          const box = document.querySelector('#result[data-kind]');
          let result = null;
          if (box && visible(box)) {
            result = {kind:box.dataset.kind};
            for(const e of box.querySelectorAll('[data-field]')) if(visible(e)) result[e.dataset.field]=e.textContent;
          }
          return {url:location.href,title:document.title,text:document.body.innerText.slice(0,14000),
            controls,fields,errors:Array.from(document.querySelectorAll('[data-errors]')).map(x=>x.innerText).filter(Boolean),result};
        }""")
        return Observation.model_validate(data)

    async def extract(self):
        rows = await self.page.get_by_role("table", name="Catalog results").locator("tbody tr").evaluate_all("""rows => rows.map(r => ({
          id:r.querySelector('[data-column=id]').textContent.trim(),
          name:r.querySelector('[data-column=name]').textContent.trim(),
          category:r.querySelector('[data-column=category]').textContent.trim(),
          price:Number(r.querySelector('[data-column=price]').textContent),
          source_url:r.querySelector('a').href
        }))""")
        for row in rows:
            self.policy.check(row["source_url"])
        return rows

    async def screenshot(self, number):
        name = f"{self.run_id}-{number:03d}.png"
        path = self.settings.runtime_dir / "screenshots" / name
        # Text traces use redaction; screenshots mask every input and private receipt field.
        await self.page.screenshot(path=str(path), full_page=False,
                                   mask=[self.page.locator("input,textarea,[data-private],aside")],
                                   mask_color="#c7d9d3")
        return name

    async def review_proposal(self, action):
        obs = await self.inspect_page()
        payload = {"action": action.model_dump(), "url": self.page.url,
                   "fields": obs.fields, "effect": "Create one SIMULATED local booking"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload | {"digest": digest}

    async def execute(self, action, screenshot_number=0):
        # Validate even when called outside the agent with an untyped dictionary.
        action = TypeAdapter(Action).validate_python(action)
        tool = action.tool
        if tool == "navigate":
            await self.page.goto(self.policy.check(action.url), wait_until="domcontentloaded")
        elif tool == "inspect_page":
            return (await self.inspect_page()).model_dump()
        elif tool in {"click", "fill", "select_option"}:
            obs = await self.inspect_page()
            # Locator descriptions must occur in the latest observation.
            if not any(c.target == action.target for c in obs.controls):
                raise LookupError(f"Target is absent from the fresh observation: {action.target.name}")
            loc = self.locator(action.target)
            if await loc.count() != 1:
                raise LookupError("Target must resolve to exactly one element")
            if tool == "fill":
                if action.target.by != "label" or action.target.name not in obs.fields:
                    raise ValueError("Fill requires an observed labeled input")
                if await loc.get_attribute("type") == "password":
                    raise PermissionError("Password entry is outside the demo scope")
                await loc.fill(action.value)
            elif tool == "select_option":
                await loc.select_option(label=action.value)
            else:
                href = await loc.get_attribute("href")
                if href:
                    from urllib.parse import urljoin
                    self.policy.check(urljoin(self.page.url, href))
                effect = await loc.get_attribute("data-effect")
                if effect == "booking":
                    proposal = await self.review_proposal(action)
                    if self.require_review and proposal["digest"] != self.authorized_digest:
                        raise ReviewRequired(proposal)
                    self.authorized_digest = None  # one exact action, consumed once
                if effect:
                    if effect in self.attempted_effects:
                        raise DuplicateSubmission("Submission already attempted; inspect or reload its receipt")
                    self.attempted_effects.add(effect)
                    async with self.page.expect_response(lambda r: r.request.method == "POST" and
                                                         r.url.startswith(self.start_url + "/")) as response:
                        await loc.click()
                    await (await response.value).body()
                    await self.page.locator('#result[data-kind], [data-errors]:not(:empty)').first.wait_for()
                else:
                    await loc.click()
                    await self.page.wait_for_load_state("domcontentloaded")
                self.policy.check(self.page.url)
        elif tool == "scroll":
            await self.page.mouse.wheel(0, 600 if action.direction == "down" else -600)
        elif tool == "extract_structured_data":
            return await self.extract()
        elif tool == "capture_screenshot":
            return await self.screenshot(screenshot_number)
        elif tool == "finish_task":
            return {"requested_finish": action.reason}
        return {"executed": tool}
