from __future__ import annotations

import asyncio
import html
import re
import secrets
from typing import Any

from .base import Resolver
from ..browser_observer import available as browser_available, observe_player
from ..bytedance import collect_candidates, dedupe_candidates
from ..hls import expand_hls_candidates
from ..models import LiveState, Platform, ResolveResult, StreamCandidate
from ..probe import available as ffprobe_available, deep_probe_unknown_candidates, probe_best_candidates
from ..quality import mark_recommended, short_edge, sort_best
from ..settings import settings
from ..stream_family import add_family_hypotheses

ROOM_RE = re.compile(r"live\.douyin\.com/(\d+)")
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


def _decode_page(text: str) -> str:
    return (
        html.unescape(text)
        .replace("\\u002F", "/")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )


def _meta(text: str, key: str, max_len: int) -> str | None:
    # Works on both normal JSON and common escaped hydration snippets.
    patterns = [
        rf'"{re.escape(key)}"\s*:\s*"([^"\\]{{1,{max_len}}})"',
        rf'\\"{re.escape(key)}\\"\s*:\s*\\"([^"\\]{{1,{max_len}}})',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return html.unescape(m.group(1))
    return None


class DouyinResolver(Resolver):
    async def resolve(self, url: str) -> ResolveResult:
        canonical = url
        room_id: str | None = None
        title: str | None = None
        nickname: str | None = None
        candidates: list[StreamCandidate] = []
        diagnostics: dict[str, Any] = {
            "sources": [],
            "ffprobe_available": ffprobe_available(),
            "browser_observer_available": browser_available(),
            "pipeline": "parallel_discovery_single_probe_v043",
        }

        headers = {
            "User-Agent": DESKTOP_UA,
            "Referer": "https://live.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cookie": f"__ac_nonce={secrets.token_hex(11)[:21]}",
        }

        async def page_discovery():
            r = await self.client.get(url, follow_redirects=True, headers=headers)
            local_canonical = str(r.url)
            decoded = _decode_page(r.text)
            return {
                "canonical": local_canonical,
                "title": _meta(decoded, "title", 240),
                "nickname": _meta(decoded, "nickname", 120),
                "candidates": collect_candidates(
                    decoded,
                    source="douyin.page_state",
                    headers={"User-Agent": DESKTOP_UA, "Referer": local_canonical},
                    provenance="PAGE_STATE",
                ),
            }

        # Page hydration and the official player are independent sources. Run
        # them together instead of page->probe->browser->probe serially.
        page_task = asyncio.create_task(page_discovery())
        browser_task = (
            asyncio.create_task(observe_player(url))
            if settings.enable_browser_observer and settings.always_observe_player
            else None
        )

        try:
            page_result = await page_task
            canonical = page_result["canonical"]
            room_match = ROOM_RE.search(canonical)
            room_id = room_match.group(1) if room_match else None
            title = page_result["title"]
            nickname = page_result["nickname"]
            if page_result["candidates"]:
                diagnostics["sources"].append("DOUYIN_PAGE_STATE")
                candidates.extend(page_result["candidates"])
        except Exception as exc:
            diagnostics["page_error"] = str(exc)[:300]
            room_match = ROOM_RE.search(canonical)
            room_id = room_match.group(1) if room_match else None

        if browser_task is not None:
            try:
                observation = await browser_task
                diagnostics.update({
                    "browser_media_requests": observation.media_requests,
                    "browser_json_responses": observation.json_responses,
                    "browser_page_state_candidates": observation.page_state_candidates,
                    "browser_performance_entries": observation.performance_entries,
                    "browser_errors": observation.errors[:5],
                })
                if observation.candidates:
                    diagnostics["sources"].append("OFFICIAL_PLAYER_OBSERVER")
                    candidates.extend(observation.candidates)
            except Exception as exc:
                diagnostics["browser_errors"] = [str(exc)[:300]]

        candidates = dedupe_candidates(candidates)
        if not candidates:
            return ResolveResult(
                platform=Platform.DOUYIN,
                state=LiveState.OFFLINE,
                canonical_url=canonical,
                content_id=room_id,
                creator_name=nickname,
                title=title,
                strategy="DOUYIN_FAST_MAX_V043",
                diagnostics=diagnostics,
            )

        candidates = await expand_hls_candidates(self.client, candidates)
        if settings.enable_stream_family_probe:
            candidates = add_family_hypotheses(candidates)
        candidates = dedupe_candidates(candidates)
        candidates = await probe_best_candidates(sort_best(candidates))
        candidates = mark_recommended(candidates)

        best_edge = max((short_edge(c) for c in candidates if c.verified), default=0)
        winner = next((c for c in candidates if c.recommended), None)
        diagnostics.update({
            "candidate_count": len(candidates),
            "verified_streams": sum(1 for c in candidates if c.verified),
            "player_observed_streams": sum(1 for c in candidates if c.observed_by_player),
            "derived_verified_streams": sum(1 for c in candidates if c.derived and c.verified),
            "best_short_edge": best_edge,
            "winner": ({
                "id": winner.id,
                "width": winner.width,
                "height": winner.height,
                "fps": winner.fps,
                "codec": winner.video_codec,
                "bitrate": winner.bitrate,
                "protocol": winner.protocol,
                "source": winner.source,
                "verified": winner.verified,
                "observed_by_player": winner.observed_by_player,
            } if winner else None),
            "quality_warning": (
                None if best_edge >= settings.high_quality_short_edge
                else "No 1080-class or higher stream was VERIFIED in the active page/player session"
            ),
        })

        return ResolveResult(
            platform=Platform.DOUYIN,
            state=LiveState.LIVE,
            canonical_url=canonical,
            content_id=room_id,
            creator_name=nickname,
            title=title,
            strategy="DOUYIN_FAST_MAX_V043",
            streams=candidates,
            diagnostics=diagnostics,
        )
