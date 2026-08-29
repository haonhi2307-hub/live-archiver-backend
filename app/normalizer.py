from __future__ import annotations
import re
from urllib.parse import urlparse
from .models import Platform
from .errors import InvalidUrlError

URL_RE = re.compile(r'https?://[^\s]+')

def extract_url(raw: str) -> str:
    raw = raw.strip()
    m = URL_RE.search(raw)
    url = m.group(0) if m else raw
    return url.rstrip('.,;，。)】]')

def detect_platform(url: str) -> Platform:
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        return Platform.TIKTOK
    if "douyin.com" in host:
        return Platform.DOUYIN
    if "facebook.com" in host or "fb.watch" in host:
        return Platform.FACEBOOK
    raise InvalidUrlError(f"Unsupported URL: {url}")

def normalize(raw: str) -> tuple[Platform, str]:
    url = extract_url(raw)
    if not url.startswith(("http://", "https://")):
        raise InvalidUrlError("A full http(s) URL is required")
    return detect_platform(url), url
