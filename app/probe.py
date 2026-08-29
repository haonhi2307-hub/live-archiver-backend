from __future__ import annotations

import asyncio
import json
import shutil
from fractions import Fraction

from .models import StreamCandidate
from .quality import quality_score
from .settings import settings


def available() -> bool:
    return bool(shutil.which("ffprobe"))


def _fps(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        fps = float(Fraction(value))
        return fps if 1.0 <= fps <= 240.0 else None
    except Exception:
        return None


def _safe_header_blob(headers: dict[str, str]) -> str:
    # Candidate headers are already scrubbed, but keep a second defense layer.
    deny = {"cookie", "authorization", "proxy-authorization"}
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() not in deny)


async def _probe(candidate: StreamCandidate, *, deep: bool = False) -> StreamCandidate:
    if not settings.enable_ffprobe or not available():
        return candidate

    analyzeduration = "24000000" if deep else "8000000"
    probesize = "32000000" if deep else "12000000"
    timeout = settings.ffprobe_deep_timeout_seconds if deep else settings.ffprobe_timeout_seconds

    cmd = [
        "ffprobe", "-v", "error",
        "-rw_timeout", str(int(timeout * 1_000_000)),
        "-analyzeduration", analyzeduration,
        "-probesize", probesize,
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate",
        "-show_entries", "format=bit_rate,format_name",
        "-of", "json",
    ]
    if candidate.headers:
        blob = _safe_header_blob(candidate.headers)
        if blob:
            cmd += ["-headers", blob]
    cmd += [candidate.url]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        if proc.returncode != 0:
            msg = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            short = msg[-1][:220] if msg else f"ffprobe exit {proc.returncode}"
            return candidate.model_copy(update={
                "verified": False,
                "probe_error": short,
                "recommended": False,
            })
        data = json.loads(stdout or b"{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video:
            return candidate.model_copy(update={"probe_error": "No video stream found", "recommended": False})
        codec = video.get("codec_name") or candidate.video_codec
        width = int(video.get("width") or 0) or candidate.width
        height = int(video.get("height") or 0) or candidate.height
        fps = _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")) or candidate.fps
        format_data = data.get("format") or {}
        bitrate = int(video.get("bit_rate") or 0) or int(format_data.get("bit_rate") or 0) or candidate.bitrate
        return candidate.model_copy(update={
            "video_codec": codec,
            "audio_codec": (audio or {}).get("codec_name") or candidate.audio_codec,
            "width": width,
            "height": height,
            "fps": fps,
            "bitrate": bitrate,
            "verified": bool(width and height),
            "probe_error": None,
            "stability_score": max(candidate.stability_score or 0.0, 0.70 if deep else 0.55),
            "quality_confidence": max(candidate.quality_confidence, 0.995 if deep else 0.985),
            "quality_note": candidate.quality_note or ("Deep media probe verified" if deep else "Media probe verified"),
        })
    except asyncio.TimeoutError:
        return candidate.model_copy(update={"probe_error": f"ffprobe timeout after {timeout:g}s", "recommended": False})
    except Exception as exc:
        return candidate.model_copy(update={"probe_error": str(exc)[:220], "recommended": False})


async def probe_candidate(candidate: StreamCandidate, *, deep: bool = False) -> StreamCandidate:
    return await _probe(candidate, deep=deep)


def _probe_priority(c: StreamCandidate) -> tuple:
    unknown = 1 if not c.width or not c.height else 0
    derived = 1 if c.derived else 0
    player = 1 if c.observed_by_player else 0
    original = 1 if c.is_original else 0
    return (player, derived, unknown, original, *quality_score(c))


def _visual_key(c: StreamCandidate) -> tuple:
    codec = (c.video_codec or "").lower()
    if codec in {"h265", "hvc1", "hev1", "bytevc1"}:
        codec = "hevc"
    elif codec in {"avc", "avc1"}:
        codec = "h264"
    fps_bucket = int(round((c.fps or 0.0) / 5.0) * 5) if c.fps else 0
    return (c.width or 0, c.height or 0, fps_bucket, codec, c.protocol.lower())


def _select_probe_set(streams: list[StreamCandidate], limit: int) -> list[StreamCandidate]:
    """Pick representatives instead of probing every mirror of the same rendition."""
    ranked = sorted(streams, key=quality_score, reverse=True)
    selected: list[StreamCandidate] = []
    seen_ids: set[str] = set()
    seen_visual: set[tuple] = set()

    # 1) Best quality ladder, one representative per visual rendition/transport.
    for c in ranked:
        if len(selected) >= max(3, limit // 2):
            break
        key = _visual_key(c)
        if key in seen_visual:
            continue
        selected.append(c)
        seen_ids.add(c.id)
        seen_visual.add(key)

    # 2) Player-observed / derived / unknown paths can hide a true source. Keep a
    # small number even if their metadata duplicates an API rendition.
    discovery = sorted(streams, key=_probe_priority, reverse=True)
    for c in discovery:
        if len(selected) >= limit:
            break
        if c.id in seen_ids:
            continue
        if not (c.observed_by_player or c.derived or not c.width or not c.height or c.is_original):
            continue
        selected.append(c)
        seen_ids.add(c.id)

    # 3) Fill any spare slots from the quality ranking.
    for c in ranked:
        if len(selected) >= limit:
            break
        if c.id in seen_ids:
            continue
        selected.append(c)
        seen_ids.add(c.id)
    return selected


async def probe_best_candidates(streams: list[StreamCandidate], *, max_candidates: int | None = None) -> list[StreamCandidate]:
    if not settings.enable_ffprobe or not available() or not streams:
        return streams

    limit = max(1, max_candidates or settings.ffprobe_max_candidates)
    selected = _select_probe_set(streams, limit)
    sem = asyncio.Semaphore(max(1, settings.ffprobe_parallelism))

    async def one(c: StreamCandidate) -> StreamCandidate:
        async with sem:
            # Deep probing is expensive. Use it only when geometry is actually
            # unknown; player/derived candidates with metadata get the fast probe.
            deep = bool(not c.width or not c.height)
            return await _probe(c, deep=deep)

    probed = await asyncio.gather(*(one(c) for c in selected))
    by_id = {c.id: c for c in probed}
    return [by_id.get(c.id, c) for c in streams]


async def deep_probe_unknown_candidates(streams: list[StreamCandidate]) -> list[StreamCandidate]:
    # Kept for the audit CLI/backward compatibility. Production resolvers no
    # longer run a second deep-probe pass after probe_best_candidates.
    if not settings.enable_ffprobe or not available() or not streams:
        return streams
    unknown = [c for c in streams if (not c.width or not c.height) and not c.probe_error]
    if not unknown:
        return streams
    selected = unknown[: max(1, settings.ffprobe_deep_max_candidates)]
    sem = asyncio.Semaphore(max(1, settings.ffprobe_parallelism))

    async def one(c: StreamCandidate) -> StreamCandidate:
        async with sem:
            return await _probe(c, deep=True)

    probed = await asyncio.gather(*(one(c) for c in selected))
    by_id = {c.id: c for c in probed}
    return [by_id.get(c.id, c) for c in streams]
