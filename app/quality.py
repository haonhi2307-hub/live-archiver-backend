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


def provenance_weight(c: StreamCandidate) -> float:
    prov = (c.provenance or "").upper()
    if prov in {"PLAYER_OBSERVED", "PLAYER_REQUEST", "BROWSER_RESPONSE"}:
        return 1.0
    if prov in {"FAST_API", "UNIVERSAL_DATA", "WEBCAST_INFO", "SIGI_STATE"}:
        return 0.9
    if prov in {"AUTH_BROWSER"}:
        return 0.85
    if prov in {"ANONYMOUS", "API_FAST"}:
        return 0.7
    if c.derived or prov in {"FAMILY_DERIVED", "DERIVED"}:
        return 0.4
    return 0.5


def quality_score(c: StreamCandidate) -> tuple:
    """Rank verified real media properties first.

    - verified comes first (unverified/error candidates rank lowest).
    - pixels / short edge / fps / bitrate measure real media delivery.
    - provenance weights observed player > fast API > derived hypotheses.
    - codec is a secondary tie-breaker (high-bitrate H264 > low-bitrate HEVC).
    """
    verified = 1 if c.verified else 0
    if c.probe_error:
        verified = -1
    pixels = (c.width or 0) * (c.height or 0)
    edge = short_edge(c)
    fps = round(c.fps or 0.0)
    bitrate = c.bitrate or 0
    prov = provenance_weight(c)
    stability = c.stability_score or 0.0
    tier = quality_tier(c.platform_quality)
    codec = (c.video_codec or "").lower()
    codec_rank = 3 if codec in {"hevc", "h265", "hvc1", "hev1", "bytevc1"} else 2 if codec in {"h264", "avc", "avc1"} else 1
    protocol_rank = {"flv": 4, "hls": 3, "http": 2, "dash": 1}.get(c.protocol.lower(), 0)
    return (
        verified,
        pixels,
        edge,
        fps,
        bitrate,
        prov,
        stability,
        codec_rank,
        protocol_rank,
        tier,
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
