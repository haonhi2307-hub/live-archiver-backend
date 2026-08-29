from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

from .models import StreamCandidate

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_RES_RE = re.compile(r"(?<!\d)(\d{3,5})\s*[xX*]\s*(\d{3,5})(?!\d)")
_QUALITY_HINTS = {
    "origin", "origion", "original", "source", "uhd", "hd", "hd1", "full_hd1",
    "sd", "sd1", "sd2", "ld", "md", "ao", "od", "high", "medium", "low",
}
_JSON_STRING_KEYS = {
    "stream_data", "streamdata", "pull_data", "pulldata", "sdk_params", "sdkparams",
    "live_core_sdk_data", "livecoresdkdata", "stream_url", "streamurl", "data",
}
_URL_KEYS = {
    "flv", "hls", "url", "stream", "pull_url", "pullurl", "rtmp_pull_url",
    "hls_pull_url", "flv_pull_url", "play_url", "playurl", "main",
}


def normalize_url(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    text = text.replace("\\u002F", "/").replace("\\u0026", "&").replace("\\/", "/")
    if not text.startswith(("http://", "https://")):
        return ""
    return text.rstrip('\\')


def protocol_for(url: str, key_hint: str = "") -> str:
    low = (url + " " + key_hint).lower()
    if ".m3u8" in low or "hls" in low:
        return "hls"
    if ".flv" in low or "flv" in low or "rtmp_pull" in low:
        return "flv"
    if ".mpd" in low or "dash" in low:
        return "dash"
    return "http"


def _number(value: Any) -> int | None:
    try:
        n = int(float(value))
        return n if n > 0 else None
    except Exception:
        return None


def _float(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n > 0 else None
    except Exception:
        return None


def _resolution(value: Any) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    m = _RES_RE.search(str(value))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _codec(value: Any) -> str | None:
    text = str(value or "").lower()
    if any(x in text for x in ("bytevc1", "hevc", "h265", "hvc1", "hev1")):
        return "hevc"
    if any(x in text for x in ("h264", "avc", "avc1")):
        return "h264"
    if any(x in text for x in ("av1", "av01")):
        return "av1"
    return None


def _quality_from_path(path: tuple[str, ...]) -> str | None:
    for part in reversed(path):
        q = part.lower().replace("-", "_")
        if q in _QUALITY_HINTS or q.startswith(("origin", "uhd", "hd", "sd", "ld", "ao", "full_hd")):
            return part
    return None


def _dict_meta(node: dict[str, Any]) -> dict[str, Any]:
    width = _number(node.get("width") or node.get("video_width") or node.get("Width"))
    height = _number(node.get("height") or node.get("video_height") or node.get("Height"))
    for key in ("resolution", "candidate_resolution", "resolution_name", "size"):
        rw, rh = _resolution(node.get(key))
        width = width or rw
        height = height or rh
    fps = _float(node.get("fps") or node.get("frame_rate") or node.get("frameRate"))
    bitrate = _number(
        node.get("vbitrate") or node.get("video_bitrate") or node.get("bitrate")
        or node.get("bit_rate") or node.get("default_bitrate")
    )
    # SDK params often express bitrate in bps; preserve as-is. If it is clearly kbps, normalize.
    if bitrate and bitrate < 50_000:
        bitrate *= 1000
    codec = _codec(node.get("VCodec") or node.get("v_codec") or node.get("codec") or node.get("codec_type"))
    label = node.get("sdk_key") or node.get("name") or node.get("quality") or node.get("gear_name")
    return {"width": width, "height": height, "fps": fps, "bitrate": bitrate, "codec": codec, "label": str(label) if label else None}


def _merge_meta(parent: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    out = dict(parent)
    for k, v in local.items():
        if v not in (None, "", 0, 0.0):
            out[k] = v
    return out


def safe_media_headers(headers: dict[str, str] | None) -> dict[str, str]:
    # Never leak login/session secrets to the Android client.
    if not headers:
        return {}
    deny = {"cookie", "authorization", "proxy-authorization", "x-csrf-token", "x-xsrf-token"}
    allow = {"user-agent", "referer", "origin", "accept", "accept-language"}
    return {k: v for k, v in headers.items() if k.lower() in allow and k.lower() not in deny and v}


def collect_candidates(
    payload: Any,
    *,
    source: str,
    headers: dict[str, str] | None = None,
    provenance: str = "API",
    observed_by_player: bool = False,
) -> list[StreamCandidate]:
    """Recursively enumerate ByteDance live media URLs from arbitrary JSON.

    This intentionally does not depend on one hydration/API schema. It handles
    `stream_data` JSON strings, quality maps, generic pull URLs and SDK params.
    """
    out: list[StreamCandidate] = []
    seen: set[str] = set()
    safe_headers = safe_media_headers(headers)

    def add(url: str, path: tuple[str, ...], meta: dict[str, Any], key_hint: str) -> None:
        u = normalize_url(url)
        if not u or u in seen:
            return
        # Ignore obvious non-media assets that happen to be nested in room JSON.
        low = u.lower()
        if any(x in low for x in (".jpg", ".jpeg", ".png", ".webp", ".gif", "avatar", "cover")):
            return
        proto = protocol_for(u, key_hint)
        # For generic .html/.json API URLs, require a stream-looking key or extension.
        if proto == "http" and not any(x in key_hint.lower() for x in ("stream", "pull", "play")):
            return
        seen.add(u)
        label = meta.get("label") or _quality_from_path(path)
        is_original = bool(label and any(x in str(label).lower() for x in ("origin", "origion", "original", "source")))
        digest = hashlib.sha1((source + "|" + u).encode("utf-8", "ignore")).hexdigest()[:12]
        out.append(StreamCandidate(
            id=f"bd_{digest}",
            protocol=proto,
            url=u,
            platform_quality=str(label) if label else None,
            video_codec=meta.get("codec"),
            width=meta.get("width"),
            height=meta.get("height"),
            fps=meta.get("fps"),
            bitrate=meta.get("bitrate"),
            headers=safe_headers,
            quality_confidence=0.88 if observed_by_player else 0.68,
            source=source,
            provenance=provenance,
            is_original=is_original,
            verified=False,
            observed_by_player=observed_by_player,
            quality_note="Observed by official player" if observed_by_player else None,
        ))

    def walk(node: Any, path: tuple[str, ...], inherited: dict[str, Any]) -> None:
        if isinstance(node, dict):
            local = _merge_meta(inherited, _dict_meta(node))

            # Parse sdk_params before sibling URLs so codec/resolution/fps enrich them.
            for key, value in node.items():
                lk = str(key).lower()
                if lk in {"sdk_params", "sdkparams"} and isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, dict):
                            local = _merge_meta(local, _dict_meta(parsed))
                    except Exception:
                        pass

            for key, value in node.items():
                skey = str(key)
                lk = skey.lower()
                child_path = (*path, skey)
                # A map like flv_pull_url={FULL_HD1:url,HD1:url,...}
                if lk in {"flv_pull_url", "flvpullurl", "hls_pull_url", "hlspullurl", "pull_url", "pullurl"} and isinstance(value, dict):
                    for q, child in value.items():
                        qmeta = dict(local)
                        qmeta["label"] = str(q)
                        if isinstance(child, str):
                            add(child, (*child_path, str(q)), qmeta, lk)
                        else:
                            walk(child, (*child_path, str(q)), qmeta)
                    continue

                if isinstance(value, str):
                    u = normalize_url(value)
                    if u:
                        add(u, child_path, local, lk)
                        continue
                    if lk in _JSON_STRING_KEYS or value.lstrip().startswith(("{", "[")):
                        try:
                            parsed = json.loads(value)
                        except Exception:
                            parsed = None
                        if parsed is not None:
                            walk(parsed, child_path, local)
                            continue
                walk(value, child_path, local)
            return

        if isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, (*path, str(i)), inherited)
            return

        if isinstance(node, str):
            # Full page/player state often contains JSON-escaped media URLs
            # (`https:\\/\\/`, `\\u0026`). Decode the common transport
            # escapes before regex extraction so page-state discovery can see
            # the same FLV/HLS URL the browser player sees.
            decoded = html.unescape(node)
            # Nested hydration often double-escapes JSON, so peel a few safe
            # transport-escape layers rather than assuming one exact depth.
            for _ in range(3):
                decoded = decoded.replace("\\u002F", "/").replace("\\u002f", "/")
                decoded = decoded.replace("\\u0026", "&").replace("\\/", "/")
            decoded = decoded.replace("\\&", "&")
            for match in _URL_RE.findall(decoded):
                add(match, path, inherited, path[-1] if path else "")

    walk(payload, (), {})
    return out


def dedupe_candidates(streams: list[StreamCandidate]) -> list[StreamCandidate]:
    by_url: dict[str, StreamCandidate] = {}
    for c in streams:
        old = by_url.get(c.url)
        if old is None:
            by_url[c.url] = c
            continue
        # Merge richer metadata and player observation without trusting labels.
        updates: dict[str, Any] = {}
        for field in ("width", "height", "fps", "bitrate", "video_codec", "audio_codec", "stream_family_id", "rendition_suffix"):
            if getattr(old, field, None) in (None, 0, 0.0, "") and getattr(c, field, None) not in (None, 0, 0.0, ""):
                updates[field] = getattr(c, field)
        if c.observed_by_player and not old.observed_by_player:
            updates.update({"observed_by_player": True, "provenance": c.provenance, "source": c.source, "quality_confidence": max(old.quality_confidence, c.quality_confidence)})
        if c.is_original and not old.is_original:
            updates["is_original"] = True
        if c.verified and not old.verified:
            updates.update({
                "verified": True,
                "probe_error": None,
                "quality_confidence": max(old.quality_confidence, c.quality_confidence),
            })
        if c.stability_score is not None and (old.stability_score is None or c.stability_score > old.stability_score):
            updates["stability_score"] = c.stability_score
        # Keep only client-safe media headers even when a richer duplicate came
        # from yt-dlp/browser capture.
        merged_headers = safe_media_headers({**old.headers, **c.headers})
        if merged_headers != old.headers:
            updates["headers"] = merged_headers
        if updates:
            by_url[c.url] = old.model_copy(update=updates)
    return list(by_url.values())
