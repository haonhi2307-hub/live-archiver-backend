from __future__ import annotations
from contextlib import asynccontextmanager
from secrets import compare_digest, token_hex
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from .admin import router as admin_router
from .errors import ResolverError
from .health import health_registry
from .license import activate_license, check_access, create_licenses, handshake
from .models import (
    AuthActivateRequest,
    AuthHandshakeRequest,
    Platform,
    ResolveRequest,
    ResolveResult,
)
from .normalizer import normalize
from .probe import available as ffprobe_available
from .presentation import compact_streams
from .browser_observer import available as browser_observer_available
from .platforms.douyin import DouyinResolver
from .platforms.facebook import FacebookResolver
from .platforms.tiktok import TikTokResolver
from .settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=settings.connect_timeout_seconds)
    limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_keepalive_connections,
    )
    app.state.http = httpx.AsyncClient(timeout=timeout, limits=limits, http2=True, follow_redirects=True)
    yield
    await app.state.http.aclose()


app = FastAPI(title="Live Archiver Resolver", version="0.5.0", lifespan=lifespan)
app.include_router(admin_router)


def require_api_key(authorization: Annotated[str | None, Header()] = None):
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if authorization is None or not compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid resolver API key"})


@app.get("/health")
@app.get("/v1/health")
async def health():
    try:
        import yt_dlp
        ytdlp_version = getattr(getattr(yt_dlp, "version", None), "__version__", "installed")
    except Exception:
        ytdlp_version = None
    return {
        "ok": True,
        "version": "0.5.0",
        "platforms": [p.value for p in Platform],
        "capabilities": {
            "ffprobe": ffprobe_available(),
            "yt_dlp": ytdlp_version,
            "tiktok_multi_quality": True,
            "tiktok_auth_mode": (f"browser:{settings.tiktok_browser}" if settings.tiktok_browser else "sessionid" if settings.tiktok_sessionid else "cookie_header" if settings.tiktok_cookies else "anonymous"),
            "deep_probe_unknown": True,
            "official_player_observer": browser_observer_available(),
            "always_observe_player": settings.always_observe_player,
            "stream_family_probe": settings.enable_stream_family_probe,
            "hls_master_expansion": True,
            "verified_max_quality": True,
            "parallel_discovery": True,
            "single_probe_pass": True,
            "client_stream_limit": settings.client_stream_limit,
        },
    }


@app.get("/v1/platforms/health", dependencies=[Depends(require_api_key)])
async def platform_health():
    return health_registry.snapshot()


@app.post("/v1/auth/handshake")
async def auth_handshake(req: AuthHandshakeRequest):
    return handshake(req.device_fingerprint)


@app.post("/v1/auth/activate")
async def auth_activate(req: AuthActivateRequest):
    try:
        return activate_license(req.device_fingerprint, req.key_code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "ACTIVATION_FAILED", "message": str(e)})


@app.post("/v1/admin/generate-keys", dependencies=[Depends(require_api_key)])
async def admin_generate_keys(count: int = 1, days: int = 30, note: str = ""):
    keys = create_licenses(count=count, duration_days=days, note=note)
    return {"ok": True, "count": len(keys), "duration_days": days, "keys": keys}


async def _resolve(
    req: ResolveRequest,
    request: Request,
    response: Response,
    *,
    compact: bool = True,
    fill_transport_fallbacks: bool = False,
) -> ResolveResult:
    # 1. License access guard
    device_fp = req.device_fingerprint or request.headers.get("X-Device-Fingerprint")
    has_access, err_msg = check_access(device_fp)
    if not has_access:
        raise HTTPException(
            status_code=403,
            detail={"code": "LICENSE_EXPIRED", "message": err_msg},
        )

    platform: Platform | None = None
    request_id = token_hex(8)
    response.headers["X-Request-ID"] = request_id
    try:
        platform, url = normalize(req.url)
        resolver_cls = {
            Platform.TIKTOK: TikTokResolver,
            Platform.DOUYIN: DouyinResolver,
            Platform.FACEBOOK: FacebookResolver,
        }[platform]
        resolver = resolver_cls(request.app.state.http)
        result = await resolver.resolve(url)
        health_registry.success(platform, result.strategy)
        update = {"request_id": request_id}
        if compact and result.streams:
            update["streams"] = compact_streams(result.streams, fill_transport_fallbacks=fill_transport_fallbacks)
        return result.model_copy(update=update)
    except ResolverError as e:
        health_registry.failure(platform, str(e))
        raise HTTPException(status_code=422, detail={"code": getattr(e, "code", "UNKNOWN_ERROR"), "message": str(e), "request_id": request_id})
    except httpx.HTTPStatusError as e:
        health_registry.failure(platform, str(e))
        raise HTTPException(status_code=502, detail={"code": "UPSTREAM_HTTP_ERROR", "message": str(e), "request_id": request_id})
    except httpx.TimeoutException as e:
        health_registry.failure(platform, str(e))
        raise HTTPException(status_code=504, detail={"code": "UPSTREAM_TIMEOUT", "message": str(e), "request_id": request_id})
    except Exception as e:
        health_registry.failure(platform, str(e))
        raise HTTPException(status_code=500, detail={"code": "UNKNOWN_ERROR", "message": str(e), "request_id": request_id})


@app.post("/v1/resolve", response_model=ResolveResult, dependencies=[Depends(require_api_key)])
async def resolve(req: ResolveRequest, request: Request, response: Response):
    return await _resolve(req, request, response, compact=True)


@app.post("/v1/audit", response_model=ResolveResult, dependencies=[Depends(require_api_key)])
async def audit(req: ResolveRequest, request: Request, response: Response):
    # Same resolver as /resolve, but kept as an explicit R&D endpoint so the
    # client can request a full quality audit without a separate code path.
    return await _resolve(req, request, response, compact=False)


@app.post("/v1/refresh", response_model=ResolveResult, dependencies=[Depends(require_api_key)])
async def refresh(req: ResolveRequest, request: Request, response: Response):
    # Stateless by design: refresh re-resolves the original share/profile/live URL.
    # Keep a few transport mirrors for automatic reconnect, but the initial
    # /v1/resolve menu stays visually compact.
    return await _resolve(req, request, response, compact=True, fill_transport_fallbacks=True)
