from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Iterable

import httpx

from .normalizer import normalize
from .platforms.douyin import DouyinResolver
from .platforms.facebook import FacebookResolver
from .platforms.tiktok import TikTokResolver
from .quality import sort_best
from .settings import settings
from .models import Platform, StreamCandidate


def _fmt_rate(value: int | None) -> str:
    if not value:
        return "?"
    return f"{value / 1_000_000:.2f}M"


def _fmt_candidate(i: int, c: StreamCandidate) -> str:
    res = f"{c.width or '?'}x{c.height or '?'}"
    fps = f"{c.fps:g}" if c.fps else "?"
    flags = []
    if c.recommended:
        flags.append("WINNER")
    if c.verified:
        flags.append("VERIFIED")
    if c.observed_by_player:
        flags.append("PLAYER")
    if c.derived:
        flags.append("DERIVED")
    if c.is_original:
        flags.append("LABEL_ORIGIN")
    flag_text = ",".join(flags) or "-"
    return (
        f"{i:02d}  {res:>11}  {fps:>5}fps  {(c.video_codec or '?'):>6}  "
        f"{_fmt_rate(c.bitrate):>7}  {c.protocol:>4}  {flag_text:<32}  "
        f"{c.source or '?'}\n     {c.url}"
    )


async def _run(url: str, dump_json: bool) -> int:
    platform, normalized = normalize(url)
    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=settings.connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, http2=True, follow_redirects=True) as client:
        resolver_cls = {
            Platform.TIKTOK: TikTokResolver,
            Platform.DOUYIN: DouyinResolver,
            Platform.FACEBOOK: FacebookResolver,
        }[platform]
        result = await resolver_cls(client).resolve(normalized)

    if dump_json:
        print(result.model_dump_json(indent=2))
        return 0

    print(f"Platform : {result.platform.value}")
    print(f"State    : {result.state.value}")
    print(f"Strategy : {result.strategy}")
    print(f"URL      : {result.canonical_url}")
    print(f"Creator  : {result.creator_name or '?'}")
    print(f"Title    : {result.title or '?'}")
    print("\nCandidates (actual-media ranking):")
    print("=" * 120)
    ranked: Iterable[StreamCandidate] = sort_best(result.streams)
    for i, candidate in enumerate(ranked, 1):
        print(_fmt_candidate(i, candidate))
    print("=" * 120)
    winner = next((c for c in result.streams if c.recommended), None)
    if winner:
        print("WINNER:", _fmt_candidate(0, winner).replace("00  ", "", 1))
    else:
        print("WINNER: none")
    print("\nDiagnostics:")
    print(json.dumps(result.diagnostics, ensure_ascii=False, indent=2))
    return 0 if result.streams else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Archiver Verified-Max quality auditor")
    parser.add_argument("url", help="Public TikTok/Douyin/Facebook LIVE URL")
    parser.add_argument("--json", action="store_true", help="Print the full resolver JSON")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args.url, args.json)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"AUDIT FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
