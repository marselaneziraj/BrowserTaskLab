import pytest
from playwright.async_api import async_playwright, expect

pytestmark = pytest.mark.e2e


async def test_browser_interface_runs_task_and_fits_mobile(local_app):
    settings, _ = local_app
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(settings.base_url)
            js = await page.request.get(settings.base_url + "/static/app.js")
            assert "javascript" in js.headers["content-type"]
            await page.get_by_role("button", name="Start run").click()
            await expect(page.locator("#status")).to_have_text("succeeded", timeout=30000)
            await expect(page.locator("#result-data")).to_contain_text("Python Field Notes")
            await page.get_by_role("button", name="Evaluations", exact=True).click()
            await expect(page.get_by_role("heading", name="Evaluation lab")).to_be_visible()
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="Agent workspace", exact=True).click()
            assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert errors == []
        finally:
            await browser.close()
