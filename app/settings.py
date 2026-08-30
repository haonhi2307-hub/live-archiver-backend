from __future__ import annotations
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    api_key: str = os.getenv("LIVE_ARCHIVER_API_KEY", "").strip()
    client_api_key: str = os.getenv("LIVE_ARCHIVER_CLIENT_API_KEY", "live_archiver_client_v05").strip()
    admin_secret: str = os.getenv("ADMIN_SECRET", "").strip()
    admin_cookie_secure: bool = _bool("ADMIN_COOKIE_SECURE", False)
    trusted_proxies: str = os.getenv("LIVE_ARCHIVER_TRUSTED_PROXIES", "127.0.0.1").strip()
    lease_max_duration_seconds: int = int(os.getenv("LIVE_ARCHIVER_LEASE_MAX_SECONDS", "21600"))  # 6 hours

    request_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_REQUEST_TIMEOUT", "20"))
    connect_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_CONNECT_TIMEOUT", "10"))
    max_connections: int = int(os.getenv("LIVE_ARCHIVER_MAX_CONNECTIONS", "200"))
    max_keepalive_connections: int = int(os.getenv("LIVE_ARCHIVER_MAX_KEEPALIVE", "50"))

    # Optional TikTok authenticated resolver modes. Secrets stay server-side.
    tiktok_cookies: str = os.getenv("LIVE_ARCHIVER_TIKTOK_COOKIES", "").strip()
    tiktok_sessionid: str = os.getenv("LIVE_ARCHIVER_TIKTOK_SESSIONID", "").strip()
    tiktok_tt_target_idc: str = os.getenv("LIVE_ARCHIVER_TIKTOK_TT_TARGET_IDC", "").strip()
    tiktok_browser: str = os.getenv("LIVE_ARCHIVER_TIKTOK_BROWSER", "").strip().lower()
    tiktok_browser_profile: str = os.getenv("LIVE_ARCHIVER_TIKTOK_BROWSER_PROFILE", "").strip()

    # Media verification.
    enable_ytdlp_fallback: bool = _bool("LIVE_ARCHIVER_ENABLE_YTDLP_FALLBACK", True)
    enable_ffprobe: bool = _bool("LIVE_ARCHIVER_ENABLE_FFPROBE", True)
    ffprobe_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_FFPROBE_TIMEOUT", "4.0"))
    ffprobe_max_candidates: int = int(os.getenv("LIVE_ARCHIVER_FFPROBE_MAX", "12"))
    ffprobe_max_concurrency: int = int(os.getenv("LIVE_ARCHIVER_FFPROBE_MAX_CONCURRENCY", "4"))
    ffprobe_deep_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_FFPROBE_DEEP_TIMEOUT", "6.0"))
    ffprobe_deep_max_candidates: int = int(os.getenv("LIVE_ARCHIVER_FFPROBE_DEEP_MAX", "4"))
    ffprobe_parallelism: int = int(os.getenv("LIVE_ARCHIVER_FFPROBE_PARALLEL", "4"))

    # If a fast/API resolver cannot verify at least this short edge, invoke the
    # official-player observer instead of assuming 720p is the maximum.
    high_quality_short_edge: int = int(os.getenv("LIVE_ARCHIVER_HQ_EDGE", "900"))
    tiktok_high_quality_short_edge: int = int(os.getenv("LIVE_ARCHIVER_TIKTOK_HQ_EDGE", os.getenv("LIVE_ARCHIVER_HQ_EDGE", "900")))

    # Official web-player observation (Playwright). App-owned profile by default.
    enable_browser_observer: bool = _bool("LIVE_ARCHIVER_ENABLE_BROWSER_OBSERVER", True)
    always_observe_player: bool = _bool("LIVE_ARCHIVER_ALWAYS_OBSERVE_PLAYER", False)
    browser_max_contexts: int = int(os.getenv("LIVE_ARCHIVER_BROWSER_MAX_CONTEXTS", "2"))
    browser_observer_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_BROWSER_OBSERVER_TIMEOUT", "8.0"))
    browser_observer_seconds: float = float(os.getenv("LIVE_ARCHIVER_BROWSER_OBSERVER_SECONDS", "5.0"))
    browser_navigation_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_BROWSER_NAV_TIMEOUT", "15"))
    browser_headless: bool = _bool("LIVE_ARCHIVER_BROWSER_HEADLESS", True)
    browser_channel: str = os.getenv("LIVE_ARCHIVER_BROWSER_CHANNEL", "").strip()  # e.g. chrome
    browser_executable_path: str = os.getenv("LIVE_ARCHIVER_BROWSER_EXECUTABLE", "").strip()
    browser_profile_dir: str = os.getenv("LIVE_ARCHIVER_BROWSER_PROFILE_DIR", "./data/browser_profile").strip()
    browser_max_response_bytes: int = int(os.getenv("LIVE_ARCHIVER_BROWSER_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
    browser_user_agent: str = os.getenv(
        "LIVE_ARCHIVER_BROWSER_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    ).strip()


    # Production response / latency controls. /v1/audit remains exhaustive.
    client_stream_limit: int = int(os.getenv("LIVE_ARCHIVER_CLIENT_STREAM_LIMIT", "4"))
    ytdlp_fallback_timeout_seconds: float = float(os.getenv("LIVE_ARCHIVER_YTDLP_TIMEOUT", "12"))
    hls_max_manifests: int = int(os.getenv("LIVE_ARCHIVER_HLS_MAX_MANIFESTS", "6"))
    hls_fetch_parallelism: int = int(os.getenv("LIVE_ARCHIVER_HLS_PARALLEL", "4"))

    # Conservative ByteDance stream-family discovery. Derived siblings are
    # never used unless a real media probe succeeds.
    enable_stream_family_probe: bool = _bool("LIVE_ARCHIVER_ENABLE_STREAM_FAMILY_PROBE", True)


settings = Settings()
