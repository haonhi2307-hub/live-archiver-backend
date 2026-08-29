from __future__ import annotations
import html, json, re
from .base import Resolver
from ..models import ResolveResult, StreamCandidate, Platform, LiveState
from ..quality import sort_best

VIDEO_PATTERNS = [
    re.compile(r"[?&]v=(\d+)"),
    re.compile(r"/videos/(\d+)"),
    re.compile(r"/watch/live/.*?[?&]v=(\d+)"),
]

def _video_id(url: str) -> str | None:
    for p in VIDEO_PATTERNS:
        m = p.search(url)
        if m: return m.group(1)
    return None

def _unescape_url(s: str) -> str:
    s = html.unescape(s)
    s = s.replace('\\/', '/').replace('\\u0025', '%').replace('\\u0026', '&')
    return s

class FacebookResolver(Resolver):
    async def resolve(self, url: str) -> ResolveResult:
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"}
        r = await self.client.get(url, follow_redirects=True, headers=headers)
        canonical = str(r.url)
        text = r.text
        vid = _video_id(canonical)
        candidates: list[StreamCandidate] = []

        # Progressive legacy/new fields.
        for key, label in [
            ("playable_url_quality_hd", "hd"), ("browser_native_hd_url", "native_hd"),
            ("playable_url", "sd"), ("browser_native_sd_url", "native_sd")]:
            for pat in [rf'"{key}"\s*:\s*"([^"]+)"', rf'\\"{key}\\"\s*:\s*\\"([^\\"]+)']:
                m = re.search(pat, text)
                if m:
                    u = _unescape_url(m.group(1))
                    if u.startswith("http"):
                        candidates.append(StreamCandidate(id=f"fb_{label}", protocol="http", url=u, platform_quality=label, quality_confidence=0.4,
                            headers={"User-Agent": "facebookexternalhit/1.1"}))
                        break

        # HLS/DASH URLs inside delivery response fragments.
        for proto, key in [("hls", "hls_playlist_urls"), ("dash", "dash_manifest_urls")]:
            # Capture any URL after the key within a bounded region.
            pos = text.find(key)
            if pos >= 0:
                region = text[pos:pos+50000]
                for u in re.findall(r'https?:\\?/\\?/[^"\\\s]+', region):
                    clean = _unescape_url(u)
                    if (proto == "hls" and ".m3u8" in clean) or (proto == "dash" and ".mpd" in clean):
                        candidates.append(StreamCandidate(id=f"fb_{proto}_{len(candidates)}", protocol=proto, url=clean, platform_quality=proto, quality_confidence=0.5,
                            headers={"User-Agent": "facebookexternalhit/1.1"}))

        # Embedded DASH manifest can be present as XML text. We expose a diagnostic instead of trying to write temp files server-side in MVP.
        embedded_dash = "<MPD" in text or "&lt;MPD" in text
        state = LiveState.LIVE if candidates else LiveState.STREAM_UNAVAILABLE
        return ResolveResult(
            platform=Platform.FACEBOOK, state=state, canonical_url=canonical,
            content_id=vid, strategy="FB_PAGE_DATA", streams=sort_best(candidates),
            diagnostics={"embedded_dash_seen": embedded_dash, "fallback_needed": not bool(candidates)},
        )
