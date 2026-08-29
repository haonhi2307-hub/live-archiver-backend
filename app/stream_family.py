from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from .models import StreamCandidate

# ByteDance stream names commonly look like stream-<id>[_rendition].flv.
# Derived URLs are NEVER trusted as valid/original until a media probe succeeds.
_STREAM_RE = re.compile(
    r"(?P<prefix>(?:stream|live)-(?P<id>\d+))(?P<suffix>_[A-Za-z0-9_-]+)?(?P<ext>\.(?:flv|m3u8))$",
    re.IGNORECASE,
)

# Conservative rendition suffixes observed in public stream URLs. Keep this narrow:
# generation is a discovery hint, not a bypass and never overrides probe results.
_RENDITION_HINT_RE = re.compile(
    r"^(?:_or\d+|_origin|_origion|_uhd\d*|_hd\d*|_sd\d*|_ld\d*|_md\d*|_ao\d*|_full_hd\d*)$",
    re.IGNORECASE,
)


def stream_family(url: str) -> tuple[str | None, str | None]:
    try:
        path = urlsplit(url).path
    except Exception:
        return None, None
    name = path.rsplit("/", 1)[-1]
    m = _STREAM_RE.search(name)
    if not m:
        return None, None
    return m.group("id"), (m.group("suffix") or "")


def enrich_family(candidate: StreamCandidate) -> StreamCandidate:
    family, suffix = stream_family(candidate.url)
    updates = {}
    if family and not candidate.stream_family_id:
        updates["stream_family_id"] = family
    if suffix and not candidate.rendition_suffix:
        updates["rendition_suffix"] = suffix.lstrip("_")
    return candidate.model_copy(update=updates) if updates else candidate


def derive_base_candidate(candidate: StreamCandidate) -> StreamCandidate | None:
    """Generate an unsuffixed sibling as a *hypothesis* only.

    Several ByteDance recorder reports show API renditions such as `_or4.flv`
    while the player can request `stream-<id>.flv`. We therefore probe the
    unsuffixed sibling when the observed suffix is a known rendition hint.
    It is never marked original/verified merely because the filename looks base.
    """
    try:
        split = urlsplit(candidate.url)
        head, sep, name = split.path.rpartition("/")
        m = _STREAM_RE.search(name)
        if not m:
            return None
        suffix = m.group("suffix") or ""
        if not suffix or not _RENDITION_HINT_RE.match(suffix):
            return None
        base_name = f"{m.group('prefix')}{m.group('ext')}"
        new_path = f"{head}{sep}{base_name}" if sep else base_name
        new_url = urlunsplit((split.scheme, split.netloc, new_path, split.query, split.fragment))
        if new_url == candidate.url:
            return None
        family = m.group("id")
        return candidate.model_copy(update={
            "id": f"{candidate.id}_family_base",
            "url": new_url,
            "platform_quality": "player/base-candidate",
            "source": "stream_family_derived",
            "provenance": "DERIVED",
            "is_original": False,
            "verified": False,
            "recommended": False,
            "quality_confidence": min(candidate.quality_confidence, 0.20),
            "quality_note": "Derived unsuffixed sibling; must probe successfully before use",
            "observed_by_player": False,
            "derived": True,
            "stream_family_id": family,
            "rendition_suffix": "",
            "probe_error": None,
        })
    except Exception:
        return None


def add_family_hypotheses(streams: list[StreamCandidate]) -> list[StreamCandidate]:
    out: list[StreamCandidate] = []
    seen: set[str] = set()
    for raw in streams:
        candidate = enrich_family(raw)
        if candidate.url not in seen:
            seen.add(candidate.url)
            out.append(candidate)
        derived = derive_base_candidate(candidate)
        if derived and derived.url not in seen:
            seen.add(derived.url)
            out.append(derived)
    return out
