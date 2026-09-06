"""Playwright helpers shared by browser adapters: context factory (proxy, headless), human-like typing,
screenshots persisted per application step."""
from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Screenshot
from app.logging import get_logger

log = get_logger("submission.browser")


def proxy_for_client(client_id: int | None = None) -> dict | None:
    """Per-client proxy assignment stub: today everyone shares PROXY_URL; later map client_id -> proxy."""
    url = get_settings().proxy_url
    if not url:
        return None
    return {"server": url}


@asynccontextmanager
async def browser_page(client_id: int | None = None, headless: bool | None = None):
    from playwright.async_api import async_playwright

    settings = get_settings()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.headless if headless is None else headless, proxy=proxy_for_client(client_id))
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def human_type(locator, text: str, min_ms: int = 50, max_ms: int = 150) -> None:
    """Type with per-character delay (50-150ms) like a person would."""
    await locator.click()
    try:
        await locator.fill("")
    except Exception:  # noqa: BLE001 - some inputs reject fill; just type
        pass
    for ch in text:
        await locator.type(ch, delay=0)
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)


async def pause(a: float = 0.4, b: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def snap(page, session: Session | None, application_id: int, step: str, full_page: bool = False) -> str:
    d = get_settings().screenshot_dir / str(application_id)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.png"))) + 1
    path = d / f"{n:02d}_{step}.png"
    try:
        await page.screenshot(path=str(path), full_page=full_page)
    except Exception as e:  # noqa: BLE001
        log.warning("screenshot_failed", step=step, error=str(e))
        return ""
    if session is not None:
        session.add(Screenshot(application_id=application_id, step=step, path=str(path)))
        session.flush()
    return str(path)
