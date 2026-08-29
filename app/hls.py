from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx

from .models import StreamCandidate
from .quality import sort_best
from .settings import settings

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', re.IGNORECASE)


def _attrs(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in _ATTR_RE.findall(line):
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        result[key.upper()] = value
    return result


def parse_master_playlist(text: str, base_url: str, parent: StreamCandidate) -> list[StreamCandidate]:
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    out: list[StreamCandidate] = []
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _attrs(line.split(":", 1)[1])
        uri = next((x for x in lines[i + 1:] if not x.startswith("#")), "")
        if not uri:
            continue
        width = height = None
        res = attrs.get("RESOLUTION", "")
        if "x" in res.lower():
            try:
                w, h = re.split("[xX]", res, maxsplit=1)
                width, height = int(w), int(h)
            except Exception:
                width = height = None
        try:
            fps = float(attrs.get("FRAME-RATE", "0") or 0) or parent.fps
        except Exception:
            fps = parent.fps
        try:
            bitrate = int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0) or parent.bitrate
        except Exception:
            bitrate = parent.bitrate
        codecs = attrs.get("CODECS", "").lower()
        vcodec = parent.video_codec
        acodec = parent.audio_codec
        if any(x in codecs for x in ("hvc1", "hev1", "hevc")):
            vcodec = "hevc"
        elif "avc1" in codecs:
            vcodec = "h264"
        if "mp4a" in codecs:
            acodec = "aac"
        out.append(parent.model_copy(update={
            "id": f"{parent.id}_hls_{len(out)+1}",
            "url": urljoin(base_url, uri),
            "width": width or parent.width,
            "height": height or parent.height,
            "fps": fps,
            "bitrate": bitrate,
            "video_codec": vcodec,
            "audio_codec": acodec,
            "source": f"{parent.source or 'hls'}.master_variant",
            "provenance": parent.provenance or "MANIFEST",
            "manifest_parent": parent.url,
            "quality_confidence": max(parent.quality_confidence, 0.86),
            "verified": False,
            "recommended": False,
        }))
    return out


async def expand_hls_candidates(
    client: httpx.AsyncClient,
    streams: list[StreamCandidate],
    *,
    max_manifests: int | None = None,
    max_depth: int = 2,
) -> list[StreamCandidate]:
    """Expand HLS ladders in small concurrent batches.

    v0.4.2 fetched up to 12 manifests serially; on slow CDNs this alone could
    add minutes. v0.4.3 preserves recursive discovery but caps and parallelizes
    it because only the quality ladder, not every CDN mirror, is needed.
    """
    limit = max(1, max_manifests or settings.hls_max_manifests)
    out = list(streams)
    seen = {c.url for c in out}
    queue: list[tuple[StreamCandidate, int]] = [
        (c, 0) for c in sort_best([x for x in streams if x.protocol.lower() == "hls"])
    ]
    fetched = 0

    async def fetch_one(parent: StreamCandidate, depth: int):
        try:
            r = await client.get(parent.url, headers=parent.headers, follow_redirects=True, timeout=6.0)
            if r.status_code >= 400 or "#EXT-X-STREAM-INF" not in r.text:
                return parent, depth, []
            return parent, depth, sort_best(parse_master_playlist(r.text, str(r.url), parent))
        except Exception:
            return parent, depth, []

    parallel = max(1, settings.hls_fetch_parallelism)
    while queue and fetched < limit:
        take = min(parallel, limit - fetched, len(queue))
        batch = [queue.pop(0) for _ in range(take)]
        results = await asyncio.gather(*(fetch_one(parent, depth) for parent, depth in batch))
        fetched += len(batch)
        for _parent, depth, children in results:
            for child in children:
                if child.url in seen:
                    continue
                seen.add(child.url)
                out.append(child)
                if depth < max_depth:
                    queue.append((child, depth + 1))
    return out
