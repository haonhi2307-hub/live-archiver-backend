from __future__ import annotations

from .models import StreamCandidate
from .quality import sort_best
from .settings import settings


def _fps_bucket(value: float | None) -> int:
    if not value:
        return 0
    # Avoid duplicate cards caused by 29.97/30 or 59.94/60 metadata variants.
    return int(round(value / 5.0) * 5)


def _codec_family(value: str | None) -> str:
    low = (value or "").lower()
    if low in {"hevc", "h265", "hvc1", "hev1", "bytevc1"}:
        return "hevc"
    if low in {"h264", "avc", "avc1"}:
        return "h264"
    return low or "unknown"


def compact_streams(
    streams: list[StreamCandidate],
    limit: int | None = None,
    *,
    fill_transport_fallbacks: bool = False,
) -> list[StreamCandidate]:
    """Return only the most useful choices for the phone UI / reconnect path.

    The full candidate set is still available from /v1/audit. /v1/resolve and
    /v1/refresh intentionally avoid returning dozens of CDN mirrors that are
    visually identical and painful to scroll on a phone.
    """
    if not streams:
        return []
    max_items = max(1, limit or settings.client_stream_limit)
    ranked = sort_best(streams)

    # Keep the actual winner first even if its geometry duplicates another URL.
    winner = next((c for c in ranked if c.recommended), ranked[0])
    out: list[StreamCandidate] = [winner]
    seen_ids = {winner.id}
    seen_visual = {
        (winner.width or 0, winner.height or 0, _fps_bucket(winner.fps), _codec_family(winner.video_codec))
    }

    # Prefer verified playable alternatives with a genuinely different visual
    # quality/codec. Protocol/CDN mirror differences alone do not deserve a card.
    for c in ranked:
        if len(out) >= max_items:
            break
        if c.id in seen_ids or not c.verified or c.probe_error:
            continue
        key = (c.width or 0, c.height or 0, _fps_bucket(c.fps), _codec_family(c.video_codec))
        if key in seen_visual:
            continue
        out.append(c)
        seen_ids.add(c.id)
        seen_visual.add(key)

    # /v1/resolve deliberately stops here: a phone should not display five
    # CDN/protocol mirrors of the same picture quality. /v1/refresh can opt in
    # to transport fallbacks because those entries are used internally and are
    # not a scrollable quality menu.
    if fill_transport_fallbacks:
        for c in ranked:
            if len(out) >= max_items:
                break
            if c.id in seen_ids or not c.verified or c.probe_error:
                continue
            out.append(c)
            seen_ids.add(c.id)

    return out
