from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bytedance import collect_candidates, dedupe_candidates
from .models import StreamCandidate
from .settings import settings


# One persistent Chromium profile cannot be safely opened by two Playwright
# contexts at once. UI resolve + recorder refresh can overlap, so serialize only
# the official-player observation stage instead of letting Chromium corrupt/lock
# the shared profile.
_browser_profile_lock = asyncio.Lock()


@dataclass
class BrowserObservation:
    candidates: list[StreamCandidate] = field(default_factory=list)
    json_responses: int = 0
    media_requests: int = 0
    page_state_candidates: int = 0
    performance_entries: int = 0
    errors: list[str] = field(default_factory=list)


def package_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


def _system_browser() -> str | None:
    explicit = settings.browser_executable_path
    if explicit and Path(explicit).exists():
        return explicit
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        for path in candidates:
            if str(path) and path.exists():
                return str(path)
    return None


def available() -> bool:
    # The package alone is not enough: Playwright needs either an installed
    # bundled browser, a configured browser channel, or a system Chromium.
    if not package_available():
        return False
    if settings.browser_channel:
        return True
    if _system_browser():
        return True
    cache_roots = [
        Path.home() / ".cache/ms-playwright",
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright" if os.environ.get("LOCALAPPDATA") else None,
    ]
    for root in [x for x in cache_roots if x]:
        if root.exists() and any(root.glob("chromium-*")):
            return True
    return False


def should_capture_response(url: str, content_type: str = "") -> bool:
    low = url.lower()
    ctype = content_type.lower()
    return (
        "webcast/room" in low
        or "api-live" in low
        or ("/live/" in low and "api" in low)
        or "room/web/enter" in low
        or "application/json" in ctype
    )


def is_media_url(url: str) -> bool:
    low = url.lower()
    return any(x in low for x in (".flv", ".m3u8", ".mpd")) and low.startswith(("http://", "https://"))


async def observe_player(url: str, *, seconds: float | None = None) -> BrowserObservation:
    """Passively observe media/room data requested by the official web player."""
    obs = BrowserObservation()
    if not settings.enable_browser_observer:
        obs.errors.append("browser observer disabled")
        return obs
    if not package_available():
        obs.errors.append("playwright package not installed")
        return obs

    from playwright.async_api import async_playwright

    capture_seconds = seconds if seconds is not None else settings.browser_observer_seconds
    profile = Path(settings.browser_profile_dir).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    pending: set[asyncio.Task] = set()
    accept_events = True

    async def handle_request(request) -> None:
        try:
            req_url = str(request.url or "")
            if not is_media_url(req_url):
                return
            headers = await request.all_headers()
            candidates = collect_candidates(
                {"stream": req_url},
                source="official_player.network",
                headers=headers,
                provenance="PLAYER_REQUEST",
                observed_by_player=True,
            )
            obs.candidates.extend(candidates)
            obs.media_requests += len(candidates)
        except Exception as exc:
            if len(obs.errors) < 10:
                obs.errors.append(f"request capture: {exc}")

    async def handle_response(response) -> None:
        try:
            ctype = str((await response.all_headers()).get("content-type") or "")
            if not should_capture_response(str(response.url), ctype):
                return
            body = await response.body()
            if not body or len(body) > settings.browser_max_response_bytes:
                return
            text = body.decode("utf-8", "ignore")
            if not any(x in text for x in ("stream_data", "pull_data", "flv_pull_url", "hls_pull_url", "live_core_sdk_data", "rtmp_pull_url", ".flv", ".m3u8")):
                return
            try:
                payload: Any = json.loads(text)
            except Exception:
                payload = text
            headers = await response.request.all_headers()
            candidates = collect_candidates(
                payload,
                source="official_player.response",
                headers=headers,
                provenance="BROWSER_RESPONSE",
                observed_by_player=True,
            )
            if candidates:
                obs.candidates.extend(candidates)
                obs.json_responses += 1
        except Exception as exc:
            if len(obs.errors) < 10:
                obs.errors.append(f"response capture: {exc}")

    def schedule(coro) -> None:
        if not accept_events:
            # Playwright may emit a final event while Chromium is closing. Do not
            # create a task that can outlive the event loop; explicitly close the
            # coroutine object we were handed.
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return
        task = asyncio.create_task(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        async with _browser_profile_lock:
            async with async_playwright() as p:
                launch_kwargs: dict[str, Any] = {
                    "headless": settings.browser_headless,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "zh-CN",
                    "user_agent": settings.browser_user_agent,
                    "args": [
                        "--autoplay-policy=no-user-gesture-required",
                        *(["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []),
                    ],
                }
                if settings.browser_channel:
                    launch_kwargs["channel"] = settings.browser_channel
                elif _system_browser():
                    launch_kwargs["executable_path"] = _system_browser()
                context = await p.chromium.launch_persistent_context(str(profile), **launch_kwargs)
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    page.on("request", lambda request: schedule(handle_request(request)))
                    page.on("response", lambda response: schedule(handle_response(response)))
                    await page.goto(url, wait_until="domcontentloaded", timeout=int(settings.browser_navigation_timeout_seconds * 1000))
                    try:
                        await page.evaluate("""() => { for (const v of document.querySelectorAll('video')) { v.muted = true; v.play().catch(()=>{}); } }""")
                    except Exception:
                        pass
                    await asyncio.sleep(max(1.0, capture_seconds))

                    # Mine the post-hydration DOM/HTML as a second independent source.
                    # Douyin reports have shown an unsuffixed HEVC source embedded in
                    # player state even when the room API only exposes an `_or4` H264 URL.
                    try:
                        content = await page.content()
                        page_candidates = collect_candidates(
                            content,
                            source="official_player.page_state",
                            headers={"User-Agent": settings.browser_user_agent, "Referer": url},
                            provenance="PAGE_STATE",
                            observed_by_player=True,
                        )
                        if page_candidates:
                            obs.candidates.extend(page_candidates)
                            obs.page_state_candidates += len(page_candidates)
                    except Exception as exc:
                        if len(obs.errors) < 10:
                            obs.errors.append(f"page-state capture: {exc}")

                    # Also inspect Resource Timing. This catches media URLs requested by
                    # JS/service-worker paths which can occasionally be missed by a
                    # narrowly timed request event listener.
                    try:
                        resource_urls = await page.evaluate(
                            """() => performance.getEntriesByType('resource').map(e => e.name).filter(Boolean)"""
                        )
                        for resource_url in resource_urls or []:
                            if not is_media_url(str(resource_url)):
                                continue
                            found = collect_candidates(
                                {"stream": str(resource_url)},
                                source="official_player.performance",
                                headers={"User-Agent": settings.browser_user_agent, "Referer": url},
                                provenance="PLAYER_RESOURCE",
                                observed_by_player=True,
                            )
                            obs.candidates.extend(found)
                            obs.performance_entries += len(found)
                    except Exception as exc:
                        if len(obs.errors) < 10:
                            obs.errors.append(f"performance capture: {exc}")

                    if pending:
                        done, still_pending = await asyncio.wait(pending, timeout=3.0)
                        for task in still_pending:
                            task.cancel()
                        if still_pending:
                            await asyncio.gather(*still_pending, return_exceptions=True)
                finally:
                    accept_events = False
                    # Stop scheduling request/response body work before Chromium is
                    # torn down. This avoids Windows Proactor closed-pipe noise at
                    # interpreter/event-loop shutdown.
                    for task in list(pending):
                        if not task.done():
                            task.cancel()
                    if pending:
                        await asyncio.gather(*list(pending), return_exceptions=True)
                    await context.close()
                    # Windows Proactor may still have pipe-close callbacks queued after
                    # Chromium exits. Give them one final loop turn so shutdown does not
                    # emit misleading "Event loop is closed" tracebacks.
                    await asyncio.sleep(0.05)
    except Exception as exc:
        obs.errors.append(str(exc))

    obs.candidates = dedupe_candidates(obs.candidates)
    return obs
