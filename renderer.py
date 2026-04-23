"""Lazy Playwright Chromium renderer — one browser per process, thread-safe."""

from __future__ import annotations

import threading
from typing import Optional, Tuple

_lock = threading.Lock()
_playwright = None
_browser = None


def _ensure_browser():
    global _playwright, _browser
    if _browser is not None:
        return
    from playwright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)


def render_url(url: str, timeout_ms: int = 25_000) -> Tuple[int, str]:
    """Load URL in headless Chromium; return (http_status, html). Status 0 on failure."""
    with _lock:
        try:
            _ensure_browser()
        except Exception:
            return 0, ""
        page = _browser.new_page()
        try:
            page.set_extra_http_headers(
                {
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = int(resp.status) if resp else 0
            # Brief wait for typical client-side JSON-LD / hydration
            page.wait_for_timeout(800)
            html = page.content()
            return status, html
        except Exception:
            return 0, ""
        finally:
            try:
                page.close()
            except Exception:
                pass


def shutdown_renderer() -> None:
    """Optional cleanup (e.g. tests)."""
    global _playwright, _browser
    with _lock:
        if _browser:
            try:
                _browser.close()
            except Exception:
                pass
            _browser = None
        if _playwright:
            try:
                _playwright.stop()
            except Exception:
                pass
            _playwright = None
