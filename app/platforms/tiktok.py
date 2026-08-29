from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from typing import Any

from .base import Resolver
from ..errors import ParserChangedError
from ..models import LiveState, Platform, ResolveResult, StreamCandidate
from ..probe import available as ffprobe_available, deep_probe_unknown_candidates, probe_best_candidates
from ..quality import mark_recommended, short_edge, sort_best
from ..settings import settings
from ..browser_observer import observe_player, available as browser_available
from ..bytedance import dedupe_candidates, safe_media_headers
from ..hls import expand_hls_candidates
from ..stream_family import add_family_hypotheses

USERNAME_RE = re.compile(r"/@([^/?#]+)/live")
SCRIPT_RE = re.compile(
    r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
SIGI_RE = re.compile(r'<script[^>]+id=["\'](?:SIGI_STATE|sigi-persisted-data)["\'][^>]*>(.*?)</script>', re.I | re.S)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
# Desktop web is intentionally primary. TikTok's web player can expose a higher
# quality ladder than the mobile-shaped public room API for the same LIVE.
WEB_UA = DESKTOP_UA


def _auth_mode() -> str:
    if settings.tiktok_browser:
        return f"browser:{settings.tiktok_browser}"
    if settings.tiktok_sessionid:
        return "sessionid"
    if settings.tiktok_cookies:
        return "cookie_header"
    return "anonymous"


def _web_params() -> dict[str, str]:
    # Keep this close to TikTok's current desktop webcast client shape. The
    # extra locale/browser fields matter because TikTok can return a reduced
    # quality ladder to thin/anonymous request shapes.
    return {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "data_collection_enabled": "true",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "",
        "history_len": "8",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": "VN",
        "region": "VN",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "Asia/Ho_Chi_Minh",
        "user_is_login": "true" if _auth_mode() != "anonymous" else "false",
        "webcast_language": "en",
        "msToken": "",
    }


def _cookie_header() -> str | None:
    # Optional local-only authenticated session. Never hard-code these in the APK.
    raw = (getattr(settings, "tiktok_cookies", "") or "").strip()
    if raw:
        return raw
    sessionid = (getattr(settings, "tiktok_sessionid", "") or "").strip()
    target = (getattr(settings, "tiktok_tt_target_idc", "") or "").strip()
    parts: list[str] = []
    if sessionid:
        parts.append(f"sessionid={sessionid}")
    if target:
        parts.append(f"tt-target-idc={target}")
    return "; ".join(parts) or None


def _username(url: str) -> str | None:
    m = USERNAME_RE.search(url)
    return m.group(1) if m else None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _codec(value: Any) -> str | None:
    if not value:
        return None
    v = str(value).lower()
    if v in {"bytevc1", "hevc", "h265", "hvc1", "hev1"}:
        return "h265"
    if v in {"h264", "avc", "avc1"}:
        return "h264"
    return v


def _resolution(value: Any) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    text = str(value).lower()
    m = re.search(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _fps(params: dict[str, Any]) -> float | None:
    for key in ("fps", "FPS", "frame_rate", "frameRate", "video_fps", "VideoFps"):
        try:
            v = float(params.get(key) or 0)
            if 1 <= v <= 240:
                return v
        except Exception:
            pass
    return None


def _bitrate(params: dict[str, Any]) -> int | None:
    for key in ("vbitrate", "bitrate", "video_bitrate", "videoBitrate"):
        try:
            v = int(float(params.get(key) or 0))
            if v > 0:
                # TikTok's vbitrate is normally bits/sec. Some variants expose kbps.
                return v * 1000 if v < 50_000 else v
        except Exception:
            pass
    return None


def _is_original(label: str | None, format_id: str | None = None) -> bool:
    text = f"{label or ''} {format_id or ''}".lower()
    return any(x in text for x in ("origin", "origion", "original", "full_hd1", "uhd"))


def _sdk_meta(raw: Any) -> dict[str, Any]:
    parsed = _json(raw)
    return parsed if isinstance(parsed, dict) else {}


def _candidate(
    *,
    cid: str,
    protocol: str,
    url: str,
    quality: str | None,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    source: str,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    bitrate: int | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    confidence: float = 0.82,
) -> StreamCandidate:
    params = params or {}
    p_width, p_height = _resolution(params.get("resolution") or params.get("Resolution"))
    def _int_param(*keys: str) -> int | None:
        for key in keys:
            try:
                value = int(float(params.get(key) or 0))
                if value > 0:
                    return value
            except Exception:
                pass
        return None
    p_width = p_width or _int_param("width", "video_width", "videoWidth", "Width")
    p_height = p_height or _int_param("height", "video_height", "videoHeight", "Height")
    return StreamCandidate(
        id=cid,
        protocol=protocol,
        url=url,
        platform_quality=quality,
        video_codec=_codec(video_codec or params.get("VCodec") or params.get("vcodec")),
        audio_codec=audio_codec,
        width=width or p_width,
        height=height or p_height,
        fps=fps or _fps(params),
        bitrate=bitrate or _bitrate(params),
        headers=safe_media_headers(headers),
        quality_confidence=confidence,
        source=source,
        is_original=_is_original(quality, cid),
        quality_note="TikTok Original" if _is_original(quality, cid) else None,
    )


class TikTokResolver(Resolver):
    async def resolve(self, url: str) -> ResolveResult:
        username = _username(url)
        canonical = url
        if not username:
            r = await self.client.get(url, follow_redirects=True, headers={"User-Agent": WEB_UA})
            canonical = str(r.url)
            username = _username(canonical)
        if not username:
            raise ParserChangedError("Could not derive TikTok LIVE username")

        live_url = f"https://www.tiktok.com/@{username}/live"
        headers = {"Referer": live_url, "User-Agent": WEB_UA}
        candidates: list[StreamCandidate] = []
        diagnostics: dict[str, Any] = {
            "sources": [],
            "ffprobe_available": ffprobe_available(),
            "auth_mode": _auth_mode(),
            "pipeline": "parallel_discovery_single_probe_v043",
        }
        title: str | None = None
        creator_name: str | None = username
        creator_id: str | None = None
        room_id: str | None = None

        # v0.4.3: launch the independent discovery paths together. The old
        # resolver ran API -> yt-dlp -> HLS -> probe -> browser -> HLS -> probe
        # sequentially, which could take 4-5 minutes on a slow CDN.
        profile_task = asyncio.create_task(self._profile_metadata(username, headers))
        room_api_task = asyncio.create_task(self._room_api(username, live_url, headers))
        browser_task = (
            asyncio.create_task(observe_player(live_url))
            if settings.enable_browser_observer and settings.always_observe_player
            else None
        )

        profile_result, room_result = await asyncio.gather(
            profile_task,
            room_api_task,
            return_exceptions=True,
        )

        if isinstance(profile_result, Exception):
            diagnostics["profile_error"] = str(profile_result)[:240]
        else:
            room_id = profile_result.get("room_id")
            creator_id = profile_result.get("creator_id")
            creator_name = profile_result.get("creator_name") or creator_name
            diagnostics["room_id_source"] = profile_result.get("source")

        if isinstance(room_result, Exception):
            diagnostics["room_api_error"] = str(room_result)[:240]
        else:
            diagnostics["sources"].append("TIKTOK_ROOM_API")
            title = room_result.get("title") or title
            room_id = room_id or room_result.get("room_id")
            candidates.extend(room_result.get("streams") or [])

        # Once a room id is known, room/info and live/detail are independent and
        # are fetched concurrently rather than serially.
        if room_id:
            webcast_task = asyncio.create_task(self._webcast_room_info(room_id, username, headers))
            detail_task = asyncio.create_task(self._live_detail(room_id, username, headers))
            webcast_result, detail_result = await asyncio.gather(
                webcast_task,
                detail_task,
                return_exceptions=True,
            )
            if isinstance(webcast_result, Exception):
                diagnostics["webcast_error"] = str(webcast_result)[:240]
            elif webcast_result:
                diagnostics["sources"].append("TIKTOK_WEBCAST_ROOM_INFO")
                title = webcast_result.get("title") or title
                creator_name = (
                    (webcast_result.get("owner") or {}).get("display_id")
                    or (webcast_result.get("ownerInfo") or {}).get("uniqueId")
                    or creator_name
                )
                candidates.extend(self._parse_webcast_formats(webcast_result, headers))

            if isinstance(detail_result, Exception):
                diagnostics["live_detail_error"] = str(detail_result)[:240]
            elif detail_result:
                diagnostics["sources"].append("TIKTOK_LIVE_DETAIL")
                title = detail_result.get("title") or title
                candidates.extend(self._parse_live_detail_formats(detail_result, headers))

        observation = None
        if browser_task is not None:
            try:
                observation = await browser_task
            except Exception as exc:
                diagnostics["browser_errors"] = [str(exc)[:300]]
            if observation is not None:
                diagnostics.update({
                    "browser_observer_available": browser_available(),
                    "browser_media_requests": observation.media_requests,
                    "browser_json_responses": observation.json_responses,
                    "browser_page_state_candidates": observation.page_state_candidates,
                    "browser_performance_entries": observation.performance_entries,
                    "browser_errors": observation.errors[:5],
                })
                if observation.candidates:
                    diagnostics["sources"].append("OFFICIAL_PLAYER_OBSERVER")
                    candidates.extend(observation.candidates)

        candidates = dedupe_candidates(candidates)
        metadata_edge = max((short_edge(c) for c in candidates), default=0)

        # yt-dlp is now a fallback, not a mandatory serial step. The official
        # player has already proven able to expose the true 1080/HEVC source.
        if settings.enable_ytdlp_fallback and (not candidates or metadata_edge < settings.high_quality_short_edge):
            try:
                ytdlp = await asyncio.wait_for(
                    asyncio.to_thread(self._ytdlp_formats, live_url, headers),
                    timeout=settings.ytdlp_fallback_timeout_seconds,
                )
                if ytdlp:
                    diagnostics["sources"].append("YTDLP_LIVE_FALLBACK")
                    title = ytdlp.get("title") or title
                    creator_name = ytdlp.get("creator_name") or creator_name
                    room_id = room_id or ytdlp.get("room_id")
                    candidates.extend(ytdlp.get("streams") or [])
            except asyncio.TimeoutError:
                diagnostics["ytdlp_error"] = f"yt-dlp fallback capped at {settings.ytdlp_fallback_timeout_seconds:g}s"
            except Exception as exc:
                diagnostics["ytdlp_error"] = str(exc)[:300]

        candidates = dedupe_candidates(candidates)
        if not candidates:
            return ResolveResult(
                platform=Platform.TIKTOK,
                state=LiveState.OFFLINE,
                canonical_url=live_url,
                content_id=room_id,
                creator_id=creator_id,
                creator_name=creator_name,
                title=title,
                strategy="TIKTOK_FAST_MAX_V043",
                diagnostics=diagnostics,
            )

        # One HLS expansion + one representative probe pass for the combined
        # candidate pool. This is the main latency reduction without lowering
        # the quality ceiling.
        candidates = await expand_hls_candidates(self.client, candidates)
        if settings.enable_stream_family_probe:
            candidates = add_family_hypotheses(candidates)
        candidates = dedupe_candidates(candidates)
        candidates = await probe_best_candidates(sort_best(candidates))
        candidates = mark_recommended(candidates)

        best_edge = max((short_edge(c) for c in candidates if c.verified), default=0)
        winner = next((c for c in candidates if c.recommended), None)
        diagnostics.update({
            "best_short_edge": best_edge,
            "verified_streams": sum(1 for c in candidates if c.verified),
            "player_observed_streams": sum(1 for c in candidates if c.observed_by_player),
            "derived_verified_streams": sum(1 for c in candidates if c.derived and c.verified),
            "candidate_count": len(candidates),
            "winner": ({
                "id": winner.id, "width": winner.width, "height": winner.height,
                "fps": winner.fps, "codec": winner.video_codec, "bitrate": winner.bitrate,
                "source": winner.source, "verified": winner.verified,
                "observed_by_player": winner.observed_by_player,
            } if winner else None),
            "quality_warning": (
                None if best_edge >= settings.high_quality_short_edge
                else "Best VERIFIED stream is below 1080-class in the active session"
            ),
        })

        return ResolveResult(
            platform=Platform.TIKTOK,
            state=LiveState.LIVE,
            canonical_url=live_url,
            content_id=room_id,
            creator_id=creator_id,
            creator_name=creator_name,
            title=title,
            strategy="TIKTOK_FAST_MAX_V043",
            streams=candidates,
            diagnostics=diagnostics,
        )

    async def _profile_metadata(self, username: str, headers: dict[str, str]) -> dict[str, Any]:
        profile_url = f"https://www.tiktok.com/@{username}"
        r = await self.client.get(profile_url, headers=headers, follow_redirects=True)
        r.raise_for_status()
        text = r.text

        m = SCRIPT_RE.search(text)
        if m:
            try:
                raw = json.loads(unescape(m.group(1)))
                scope = raw.get("__DEFAULT_SCOPE__") or {}
                user = (((scope.get("webapp.user-detail") or {}).get("userInfo") or {}).get("user") or {})
                if user:
                    return {
                        "room_id": str(user.get("roomId") or "") or None,
                        "creator_id": str(user.get("id") or "") or None,
                        "creator_name": user.get("uniqueId") or username,
                        "source": "UNIVERSAL_DATA",
                    }
            except Exception:
                pass

        m = SIGI_RE.search(text)
        if m:
            try:
                data = json.loads(unescape(m.group(1)))
                users = ((data.get("UserModule") or {}).get("users") or {})
                for user in users.values() if isinstance(users, dict) else []:
                    if isinstance(user, dict) and (user.get("uniqueId") == username or user.get("roomId")):
                        return {
                            "room_id": str(user.get("roomId") or "") or None,
                            "creator_id": str(user.get("id") or "") or None,
                            "creator_name": user.get("uniqueId") or username,
                            "source": "SIGI_STATE",
                        }
            except Exception:
                pass
        return {"room_id": None, "creator_name": username, "source": "NONE"}

    async def _webcast_room_info(self, room_id: str, username: str, headers: dict[str, str]) -> dict[str, Any] | None:
        params = {**_web_params(), "room_id": room_id}
        request_headers = {
            **headers,
            "User-Agent": DESKTOP_UA,
            "Referer": f"https://www.tiktok.com/@{username}/live",
            "Origin": "https://www.tiktok.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Ua-Mobile": "?0",
        }
        if _cookie_header():
            request_headers["Cookie"] = _cookie_header()
        elif "tt-target-idc" not in self.client.cookies:
            # Anonymous TikTokLive-style default; helps keep room/info on a
            # stable datacenter without requiring account cookies.
            self.client.cookies.set("tt-target-idc", "useast1a", domain=".tiktok.com")
        # Bootstrap the web session first so any .tiktok.com cookies set by the
        # player/profile request are present when room/info is fetched.
        try:
            await self.client.get(
                f"https://www.tiktok.com/@{username}/live",
                params={"is_from_webapp": "1", "sender_device": "pc"},
                headers={"User-Agent": DESKTOP_UA, "Accept": "text/html,*/*"},
                follow_redirects=True,
            )
        except Exception:
            pass
        r = await self.client.get(
            "https://webcast.tiktok.com/webcast/room/info",
            params=params,
            headers=request_headers,
        )
        r.raise_for_status()
        raw = r.json()
        data = raw.get("data") or {}
        status = data.get("status")
        if status is not None and str(status) not in {"2", "LIVE", "live"}:
            return None
        return data if isinstance(data, dict) else None

    async def _live_detail(self, room_id: str, username: str, headers: dict[str, str]) -> dict[str, Any] | None:
        params = {**_web_params(), "roomID": room_id}
        request_headers = {
            **headers,
            "User-Agent": DESKTOP_UA,
            "Referer": f"https://www.tiktok.com/@{username}/live",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if _cookie_header():
            request_headers["Cookie"] = _cookie_header()
        r = await self.client.get(
            "https://www.tiktok.com/api/live/detail/",
            params=params,
            headers=request_headers,
            follow_redirects=True,
        )
        r.raise_for_status()
        raw = r.json()
        data = raw.get("LiveRoomInfo") or raw.get("liveRoomInfo") or raw.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("LiveRoomInfo"), dict):
            data = data["LiveRoomInfo"]
        return data if isinstance(data, dict) else None

    def _parse_live_detail_formats(self, detail: dict[str, Any], headers: dict[str, str]) -> list[StreamCandidate]:
        out: list[StreamCandidate] = []
        # Some responses embed the same stream_url structure as webcast/room/info.
        if isinstance(detail.get("stream_url"), dict):
            out.extend(self._parse_webcast_formats(detail, headers))

        live_url = detail.get("liveUrl") or detail.get("live_url")
        if isinstance(live_url, str) and live_url.startswith("http"):
            proto = "hls" if ".m3u8" in live_url.lower() else "http"
            out.append(_candidate(
                cid="tt_live_detail_origin",
                protocol=proto,
                url=live_url,
                quality="origin",
                params={},
                headers=headers,
                source="api.live.detail",
                confidence=0.97,
            ))
        return out

    def _parse_webcast_formats(self, live_info: dict[str, Any], headers: dict[str, str]) -> list[StreamCandidate]:
        out: list[StreamCandidate] = []
        stream_url = live_info.get("stream_url") or {}
        sdk_data = stream_url.get("live_core_sdk_data") or {}
        pull_data = sdk_data.get("pull_data") or {}

        # Quality metadata is sometimes present only in pull_data.options, while
        # stream_data contains the actual URLs. Merge both before ranking/probing.
        option_meta: dict[str, dict[str, Any]] = {}
        options = pull_data.get("options") or {}
        qualities = options.get("qualities") or options.get("quality_list") or []
        if isinstance(qualities, list):
            for item in qualities:
                if not isinstance(item, dict):
                    continue
                keys = [item.get("sdk_key"), item.get("name"), item.get("quality"), item.get("key")]
                meta = {
                    "resolution": item.get("resolution"),
                    "VCodec": item.get("v_codec") or item.get("vcodec"),
                    "fps": item.get("fps"),
                    "vbitrate": item.get("bitrate") or item.get("vbitrate"),
                }
                for key in keys:
                    if key:
                        option_meta[str(key).lower()] = {k: v for k, v in meta.items() if v is not None}
        default_quality = options.get("default_quality") or {}
        if isinstance(default_quality, dict):
            for key in (default_quality.get("sdk_key"), default_quality.get("name")):
                if key:
                    option_meta.setdefault(str(key).lower(), {}).update({
                        k: v for k, v in {
                            "resolution": default_quality.get("resolution"),
                            "VCodec": default_quality.get("v_codec") or default_quality.get("vcodec"),
                            "fps": default_quality.get("fps"),
                            "vbitrate": default_quality.get("bitrate") or default_quality.get("vbitrate"),
                        }.items() if v is not None
                    })

        global_extra = stream_url.get("extra") or {}
        if not isinstance(global_extra, dict):
            global_extra = {}
        origin_meta = {
            "width": global_extra.get("width"),
            "height": global_extra.get("height"),
            "fps": global_extra.get("fps"),
            "vbitrate": global_extra.get("default_bitrate") or global_extra.get("max_bitrate"),
            "VCodec": "bytevc1" if global_extra.get("bytevc1_enable") else None,
        }
        origin_meta = {k: v for k, v in origin_meta.items() if v is not None}

        raw_stream = _json(pull_data.get("stream_data"))
        stream_data = (raw_stream or {}).get("data") if isinstance(raw_stream, dict) else None
        if not isinstance(stream_data, dict) and isinstance(raw_stream, dict):
            stream_data = raw_stream

        if isinstance(stream_data, dict):
            for quality, payload in stream_data.items():
                if not isinstance(payload, dict):
                    continue
                main = payload.get("main") or payload
                if not isinstance(main, dict):
                    continue
                params = {**option_meta.get(str(quality).lower(), {}), **_sdk_meta(main.get("sdk_params"))}
                flv = main.get("flv")
                if isinstance(flv, str) and flv.startswith("http"):
                    out.append(_candidate(
                        cid=f"tt_webcast_flv_{quality}", protocol="flv", url=flv,
                        quality=str(quality), params=params, headers=headers,
                        source="webcast.stream_data", confidence=0.94,
                    ))
                hls = main.get("hls")
                if isinstance(hls, str) and hls.startswith("http"):
                    out.append(_candidate(
                        cid=f"tt_webcast_hls_{quality}", protocol="hls", url=hls,
                        quality=str(quality), params=params, headers=headers,
                        source="webcast.stream_data", confidence=0.94,
                    ))

        def params_for(group: str, key: str | None = None) -> dict[str, Any]:
            raw = _json(stream_url.get(group))
            if key is not None and isinstance(raw, dict):
                raw = _json(raw.get(key))
            return raw if isinstance(raw, dict) else {}

        flv_map = stream_url.get("flv_pull_url") or {}
        if isinstance(flv_map, dict):
            for quality, flv in flv_map.items():
                if isinstance(flv, str) and flv.startswith("http"):
                    p = {**option_meta.get(str(quality).lower(), {}), **params_for("flv_pull_url_params", str(quality))}
                    out.append(_candidate(
                        cid=f"tt_pull_flv_{quality}", protocol="flv", url=flv,
                        quality=str(quality), params=p,
                        headers=headers, source="webcast.flv_pull_url", confidence=0.93,
                    ))

        # TikTok/yt-dlp treat the generic RTMP/HLS pull entries as the source/origin
        # tier. On many rooms rtmp_pull_url is actually an HTTPS FLV URL. v0.3
        # ignored it, which could hide the highest-quality stream.
        rtmp = stream_url.get("rtmp_pull_url")
        if isinstance(rtmp, str) and rtmp.startswith(("http://", "https://")):
            p = {**origin_meta, **params_for("rtmp_pull_url_params")}
            out.append(_candidate(
                cid="tt_pull_rtmp_origin", protocol="flv", url=rtmp, quality="origin",
                params=p, headers=headers, source="webcast.rtmp_pull_url", confidence=0.96,
            ))

        hls = stream_url.get("hls_pull_url")
        if isinstance(hls, str) and hls.startswith("http"):
            p = {**origin_meta, **params_for("hls_pull_url_params")}
            out.append(_candidate(
                cid="tt_pull_hls_origin", protocol="hls", url=hls, quality="origin",
                params=p, headers=headers, source="webcast.hls_pull_url", confidence=0.94,
            ))
        hls_map = stream_url.get("hls_pull_url_map") or {}
        if isinstance(hls_map, dict):
            for quality, hls_url in hls_map.items():
                if isinstance(hls_url, str) and hls_url.startswith("http"):
                    p = {**option_meta.get(str(quality).lower(), {}), **params_for("hls_pull_url_params", str(quality))}
                    out.append(_candidate(
                        cid=f"tt_pull_hls_{quality}", protocol="hls", url=hls_url,
                        quality=str(quality), params=p, headers=headers,
                        source="webcast.hls_pull_url_map", confidence=0.92,
                    ))
        return out

    async def _room_api(self, username: str, live_url: str, headers: dict[str, str]) -> dict[str, Any]:
        params = {**_web_params(), "sourceType": "54", "uniqueId": username}
        r = await self.client.get(
            "https://www.tiktok.com/api-live/user/room/",
            params=params,
            headers={**headers, "User-Agent": DESKTOP_UA},
        )
        r.raise_for_status()
        data = r.json()
        room = data.get("liveRoom") or data.get("data", {}).get("liveRoom") or {}
        if not room:
            return {"streams": []}
        pull_data = (((room.get("streamData") or {}).get("pull_data") or {}))
        raw_stream = _json(pull_data.get("stream_data"))
        stream_data = (raw_stream or {}).get("data") if isinstance(raw_stream, dict) else None
        if not isinstance(stream_data, dict) and isinstance(raw_stream, dict):
            stream_data = raw_stream
        out: list[StreamCandidate] = []
        if isinstance(stream_data, dict):
            for quality, payload in stream_data.items():
                if not isinstance(payload, dict):
                    continue
                main = payload.get("main") or payload
                if not isinstance(main, dict):
                    continue
                params_data = _sdk_meta(main.get("sdk_params"))
                flv = main.get("flv")
                if isinstance(flv, str) and flv.startswith("http"):
                    out.append(_candidate(
                        cid=f"tt_room_flv_{quality}", protocol="flv", url=flv,
                        quality=str(quality), params=params_data, headers=headers,
                        source="room_api", confidence=0.76,
                    ))
                hls = main.get("hls")
                if isinstance(hls, str) and hls.startswith("http"):
                    out.append(_candidate(
                        cid=f"tt_room_hls_{quality}", protocol="hls", url=hls,
                        quality=str(quality), params=params_data, headers=headers,
                        source="room_api", confidence=0.76,
                    ))
        return {
            "streams": out,
            "title": room.get("title"),
            "room_id": str(room.get("id") or room.get("roomId") or "") or None,
        }

    def _ytdlp_formats(self, live_url: str, headers: dict[str, str]) -> dict[str, Any]:
        try:
            import yt_dlp
        except Exception:
            return {"streams": []}

        ytdlp_headers = dict(headers)
        if _cookie_header():
            ytdlp_headers["Cookie"] = _cookie_header() or ""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": settings.request_timeout_seconds,
            "http_headers": ytdlp_headers,
        }
        if settings.tiktok_browser:
            # yt-dlp's supported browser-cookie loader keeps credentials local
            # on the backend PC. No cookie values are written to app logs/API.
            opts["cookiesfrombrowser"] = (
                settings.tiktok_browser,
                settings.tiktok_browser_profile or None,
                None,
                None,
            )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url, download=False)
        if not isinstance(info, dict):
            return {"streams": []}

        out: list[StreamCandidate] = []
        for f in info.get("formats") or []:
            if not isinstance(f, dict):
                continue
            media_url = f.get("url")
            if not isinstance(media_url, str) or not media_url.startswith("http"):
                continue
            format_id = str(f.get("format_id") or "unknown")
            proto_raw = str(f.get("protocol") or "").lower()
            ext = str(f.get("ext") or "").lower()
            if "m3u8" in proto_raw or ext in {"m3u8", "m3u8_native"} or format_id.startswith("hls-"):
                protocol = "hls"
            elif ext == "flv" or format_id.startswith(("flv-", "rtmp-")):
                protocol = "flv"
            elif proto_raw in {"http", "https"}:
                protocol = "http"
            else:
                continue
            tbr = f.get("tbr")
            bitrate = int(float(tbr) * 1000) if tbr else None
            r_width, r_height = _resolution(f.get("resolution"))
            f_headers = {str(k): str(v) for k, v in (f.get("http_headers") or {}).items() if v is not None}
            out.append(_candidate(
                cid=f"tt_ytdlp_{format_id}",
                protocol=protocol,
                url=media_url,
                quality=format_id.split("-", 1)[-1] if "-" in format_id else format_id,
                params={},
                headers={**headers, **f_headers},
                source=(f"yt-dlp[{_auth_mode()}]" if _auth_mode() != "anonymous" else "yt-dlp"),
                width=int(f.get("width") or 0) or r_width,
                height=int(f.get("height") or 0) or r_height,
                fps=float(f.get("fps") or 0) or None,
                bitrate=bitrate,
                video_codec=f.get("vcodec"),
                audio_codec=f.get("acodec"),
                confidence=0.94,
            ))
        return {
            "streams": out,
            "title": info.get("title"),
            "creator_name": info.get("uploader") or info.get("creator"),
            "room_id": str(info.get("id") or "") or None,
        }

    @staticmethod
    def _dedupe(streams: list[StreamCandidate]) -> list[StreamCandidate]:
        return dedupe_candidates(streams)

