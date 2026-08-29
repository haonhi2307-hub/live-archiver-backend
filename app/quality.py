from __future__ import annotations
import re
from .models import StreamCandidate


def quality_tier(label: str | None) -> int:
    """Platform labels are only a late tie-breaker, never the main truth."""
    q = (label or "").lower().replace("-", "_")
    if any(x in q for x in ("origin", "origion", "original", "source")):
        return 100
    if "full_hd" in q or q in {"uhd", "fhd"}:
        return 95
    if q in {"hd1", "hd"} or "1080" in q:
        return 80
    if q in {"sd2", "sd"} or "720" in q:
        return 60
    if q in {"sd1", "ld"}:
        return 40
    m = re.search(r"(\d{3,4})p", q)
    return int(m.group(1)) // 12 if m else 0


def short_edge(c: StreamCandidate) -> int:
    if c.width and c.height:
        return min(c.width, c.height)
    return 0


def quality_score(c: StreamCandidate) -> tuple:
    """Rank actual media properties first.

    `origin`, `uhd`, `hd1`, `_or4`, etc. are hints only. A verified 1440p
    stream must beat a 720p stream even when the latter is labelled origin.
    """
    pixels = (c.width or 0) * (c.height or 0)
    edge = short_edge(c)
    fps = c.fps or 0
    verified = 1 if c.verified else 0
    observed = 1 if c.observed_by_player else 0
    bitrate = c.bitrate or 0
    stability = c.stability_score or 0.0
    tier = quality_tier(c.platform_quality)
    # HEVC is not automatically "better", but at otherwise equal measured
    # properties it is a useful source-quality tie breaker.
    codec = (c.video_codec or "").lower()
    codec_rank = 3 if codec in {"hevc", "h265", "hvc1", "hev1", "bytevc1"} else 2 if codec in {"h264", "avc", "avc1"} else 1
    protocol_rank = {"flv": 4, "hls": 3, "http": 2, "dash": 1}.get(c.protocol.lower(), 0)
    return (
        pixels,
        edge,
        fps,
        verified,
        observed,
        bitrate,
        stability,
        codec_rank,
        tier,
        protocol_rank,
        c.quality_confidence,
    )


def sort_best(streams: list[StreamCandidate]) -> list[StreamCandidate]:
    return sorted(streams, key=quality_score, reverse=True)


def mark_recommended(streams: list[StreamCandidate]) -> list[StreamCandidate]:
    if not streams:
        return []
    ranked = sort_best(streams)
    for c in ranked:
        c.recommended = False

    # Never auto-select an unverified guessed/derived URL when at least one
    # verified playable candidate exists.
    usable = [c for c in ranked if c.verified and not c.probe_error]
    if not usable:
        usable = [c for c in ranked if not c.derived]
    if not usable:
        usable = ranked

    winner = usable[0]
    winner.recommended = True
    return ranked
