from __future__ import annotations
from contextlib import asynccontextmanager
from secrets import compare_digest, token_hex
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from .admin import router as admin_router
from .update_router import router as update_router
from .errors import ResolverError
from .health import health_registry
from .license import (
    activate_license,
    check_access,
    create_licenses,
    end_recording_lease,
    handshake,
    register_installation,
    start_recording_lease,
)
from .models import (
    AuthActivateRequest,
    AuthHandshakeRequest,
    AuthRegisterInstallationRequest,
    AuthResponse,
    LeaseEndRequest,
    LeaseResponse,
    LeaseStartRequest,
    Platform,
    ResolveRequest,
    ResolveResult,
)
from .normalizer import normalize
from .probe import available as ffprobe_available
from .browser_observer import available as browser_observer_available
from .presentation import compact_streams
from .models_video import VideoResolveRequest, VideoResolveResult
from .platforms.douyin import DouyinResolver
from .platforms.douyin_video import resolve_douyin_video
from .platforms.facebook import FacebookResolver
from .platforms.multiplatform_video import resolve_multiplatform_video
from .platforms.tiktok import TikTokResolver
from .platforms.tiktok_video import resolve_tiktok_video
from .settings import settings


def create_http_client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=settings.connect_timeout_seconds)
    limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_keepalive_connections,
    )
    try:
        return httpx.AsyncClient(timeout=timeout, limits=limits, http2=True, follow_redirects=True)
    except Exception:
        return httpx.AsyncClient(timeout=timeout, limits=limits, http2=False, follow_redirects=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = create_http_client()
    yield
    if getattr(app.state, "http", None) and not app.state.http.is_closed:
        await app.state.http.aclose()


app = FastAPI(title="Live Archiver Resolver", version="0.5.0", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(update_router)


def require_client_auth(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
):
    # Public-ish client app identification token
    expected_client = settings.client_api_key or "live_archiver_client_v05"
    provided = x_api_key or ""
    if not provided and authorization and authorization.startswith("Bearer "):
        provided = authorization[7:].strip()

    # If server has legacy API_KEY set, accept it too
    if settings.api_key and compare_digest(provided, settings.api_key):
        return

    if not compare_digest(provided, expected_client):
        # Client identification check
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED_CLIENT", "message": "Client identification key invalid or missing"},
        )


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


@app.get("/v1/platforms/health", dependencies=[Depends(require_client_auth)])
async def platform_health():
    return health_registry.snapshot()


@app.post("/v1/auth/register-installation", dependencies=[Depends(require_client_auth)])
async def auth_register_installation(req: AuthRegisterInstallationRequest):
    return register_installation(req.installation_id, req.device_fingerprint, req.public_key_pem)


@app.post("/v1/auth/handshake", dependencies=[Depends(require_client_auth)])
async def auth_handshake(req: AuthHandshakeRequest):
    if req.installation_id and req.public_key_pem:
        return register_installation(req.installation_id, req.device_fingerprint, req.public_key_pem)
    return handshake(req.device_fingerprint, req.installation_id)


@app.post("/v1/auth/activate", dependencies=[Depends(require_client_auth)])
async def auth_activate(req: AuthActivateRequest):
    try:
        return activate_license(
            device_fingerprint=req.device_fingerprint,
            key_code=req.key_code,
            installation_id=req.installation_id,
            signature=req.signature,
            nonce=req.nonce,
            timestamp_utc=req.timestamp_utc,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "ACTIVATION_FAILED", "message": str(e)})


@app.post("/v1/auth/lease/start", response_model=LeaseResponse, dependencies=[Depends(require_client_auth)])
async def auth_lease_start(req: LeaseStartRequest):
    ok, msg, status, lease_info = start_recording_lease(
        installation_id=req.installation_id,
        device_fingerprint=req.device_fingerprint,
        session_id=req.session_id,
        platform=req.platform,
        canonical_url=req.canonical_url,
        signature=req.signature,
        nonce=req.nonce,
        timestamp_utc=req.timestamp_utc,
    )
    if not ok:
        raise HTTPException(status_code=403, detail={"code": status, "message": msg})
    return LeaseResponse(
        ok=True,
        lease_id=lease_info["lease_id"] if lease_info else None,
        session_id=req.session_id,
        expires_at_utc=lease_info["expires_at_utc"] if lease_info else None,
        status="ACTIVE",
        message=msg,
    )


@app.post("/v1/auth/lease/end", dependencies=[Depends(require_client_auth)])
async def auth_lease_end(req: LeaseEndRequest):
    success = end_recording_lease(req.lease_id, req.installation_id, req.session_id)
    return {"ok": success, "message": "Lease closed"}


def get_http_client(request: Request) -> httpx.AsyncClient:
    http = getattr(request.app.state, "http", None)
    if http is None or getattr(http, "is_closed", True):
        http = create_http_client()
        request.app.state.http = http
    return http


async def _resolve(
    req: ResolveRequest,
    request: Request,
    response: Response,
    *,
    compact: bool = True,
    fill_transport_fallbacks: bool = False,
) -> ResolveResult:
    # 1. Extract device & lease identity
    device_fp = req.device_fingerprint or request.headers.get("X-Device-Fingerprint")
    inst_id = req.installation_id or request.headers.get("X-Installation-Id")
    lease_id = req.lease_id or request.headers.get("X-Lease-Id")
    session_id = request.headers.get("X-Session-Id")

    # 2. Strict License & Lease Access Guard
    has_access, err_msg, status_code = check_access(
        device_fingerprint=device_fp,
        installation_id=inst_id,
        lease_id=lease_id,
        session_id=session_id,
        mode=req.mode,
    )
    if not has_access:
        raise HTTPException(
            status_code=403,
            detail={"code": status_code, "message": err_msg},
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
        client = get_http_client(request)
        resolver = resolver_cls(client)
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


@app.post("/v1/resolve", response_model=ResolveResult, dependencies=[Depends(require_client_auth)])
async def resolve(req: ResolveRequest, request: Request, response: Response):
    return await _resolve(req, request, response, compact=True)


@app.post("/v1/audit", response_model=ResolveResult, dependencies=[Depends(require_client_auth)])
async def audit(req: ResolveRequest, request: Request, response: Response):
    return await _resolve(req, request, response, compact=False)


@app.post("/v1/refresh", response_model=ResolveResult, dependencies=[Depends(require_client_auth)])
async def refresh(req: ResolveRequest, request: Request, response: Response):
    return await _resolve(req, request, response, compact=True, fill_transport_fallbacks=True)


@app.post("/v1/video/resolve", response_model=VideoResolveResult, dependencies=[Depends(require_client_auth)])
@app.post("/api/v1/video/resolve", response_model=VideoResolveResult, dependencies=[Depends(require_client_auth)])
async def resolve_video(req: VideoResolveRequest) -> VideoResolveResult:
    raw_url = req.url.strip()
    low = raw_url.lower()
    try:
        if "douyin.com" in low or "iesdouyin.com" in low:
            return await resolve_douyin_video(raw_url)
        elif "tiktok.com" in low:
            return await resolve_tiktok_video(raw_url)
        else:
            return await resolve_multiplatform_video(raw_url)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "VIDEO_RESOLVE_FAILED", "message": str(exc)})
