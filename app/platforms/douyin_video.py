from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..models_video import VideoMediaType, VideoRendition, VideoResolveResult

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
AWEME_ID_RE = re.compile(r"/(?:video|note|share/video)/(\d+)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b(\d{18,20})\b")
P_RE = re.compile(r"(?<!\d)(\d{3,4})p(?!\d)", re.IGNORECASE)
FPS_RE = re.compile(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*fps(?!\w)", re.IGNORECASE)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def _clean_url(raw: str) -> str:
    match = URL_RE.search(raw)
    return match.group(0).rstrip(".,;，。；!！?？)]}>") if match else raw.strip()

def _safe_int(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(float(val))
        except Exception:
            return 0
    return 0

def _metadata_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, (dict, list)):
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                parts.append(str(value))
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts).lower()

def infer_short_edge(text: str) -> int:
    if "4_" in text or "4k" in text or "2160" in text:
        return 2160
    if "2k" in text or "1440" in text:
        return 1440
    if "1080" in text or "high" in text:
        return 1080
    if "720" in text or "medium" in text:
        return 720
    if "540" in text:
        return 540
    if "480" in text:
        return 480
    return 0

def infer_dimensions(w: int, h: int, label: str) -> tuple[int, int]:
    short_edge = infer_short_edge(label)
    if not w or not h:
        if short_edge:
            return round(short_edge * 16 / 9), short_edge
        return w, h

    explicit_short = min(w, h)
    if short_edge and short_edge > explicit_short:
        ratio = max(w, h) / max(1, explicit_short)
        inferred_long = round(short_edge * ratio)
        if w >= h:
            return inferred_long, short_edge
        else:
            return short_edge, inferred_long
    return w, h

async def _get_ttwid(client: httpx.AsyncClient) -> str:
    try:
        url = "https://ttwid.bytedance.com/ttwid/union/register/"
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "src_subaid": 0},
            "cbUrlProtocol": "https",
            "union": True,
        }
        resp = await client.post(url, json=payload, timeout=8.0)
        return resp.cookies.get("ttwid") or ""
    except Exception:
        return ""

async def resolve_douyin_video(raw_input: str) -> VideoResolveResult:
    target_url = _clean_url(raw_input)
    diagnostics: dict[str, Any] = {"input_url": target_url}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
        ttwid = await _get_ttwid(client)
        cookie_header = f"ttwid={ttwid}; store-region=cn; store-region-src=uid;" if ttwid else "store-region=cn; store-region-src=uid;"
        
        # Follow redirect with mobile UA
        m_headers = dict(DEFAULT_HEADERS)
        m_headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15"
        m_headers["Cookie"] = cookie_header

        resp = await client.get(target_url, headers=m_headers)
        final_url = str(resp.url)
        diagnostics["final_url"] = final_url

        aweme_id_match = AWEME_ID_RE.search(final_url) or AWEME_ID_RE.search(target_url) or NUMBER_RE.search(final_url) or NUMBER_RE.search(target_url)
        aweme_id = aweme_id_match.group(1) if aweme_id_match else ""

        detail_data: dict[str, Any] = {}

        # 1. Douyin Web Detail API
        if aweme_id:
            try:
                headers = dict(DEFAULT_HEADERS)
                headers["Cookie"] = cookie_header
                query_params = {
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "aweme_id": aweme_id,
                    "update_version_code": "170400",
                    "pc_client_type": "1",
                    "version_code": "190200",
                    "version_name": "19.2.0",
                    "cookie_enabled": "true",
                    "screen_width": "3840",
                    "screen_height": "2160",
                    "browser_language": "zh-CN",
                    "browser_platform": "Win32",
                    "browser_name": "Chrome",
                    "browser_version": "131.0.0.0",
                }
                web_url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                web_resp = await client.get(web_url, params=query_params, headers=headers)
                if web_resp.status_code == 200 and web_resp.text.strip():
                    data = web_resp.json()
                    if data.get("aweme_detail"):
                        detail_data = data["aweme_detail"]
                        diagnostics["source"] = "douyin_ttwid_web_detail"
            except Exception as exc:
                diagnostics["web_detail_error"] = str(exc)

        # 2. Iesdouyin ItemInfo API Fallback
        if not detail_data and aweme_id:
            try:
                api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={aweme_id}"
                api_resp = await client.get(api_url, headers=DEFAULT_HEADERS)
                if api_resp.status_code == 200:
                    items = api_resp.json().get("item_list") or []
                    if items:
                        detail_data = items[0]
                        diagnostics["source"] = "iesdouyin_iteminfo"
            except Exception as exc:
                diagnostics["iesdouyin_error"] = str(exc)

        # 3. Mobile Feed API Fallback
        if not detail_data and aweme_id:
            try:
                feed_url = f"https://api3-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id={aweme_id}&device_platform=android&version_code=230101"
                api_resp = await client.get(feed_url, headers={"User-Agent": "com.ss.android.ugc.aweme/230101 (Linux; U; Android 14; zh_CN)"})
                if api_resp.status_code == 200:
                    aweme_list = api_resp.json().get("aweme_list") or []
                    if aweme_list:
                        detail_data = aweme_list[0]
                        diagnostics["source"] = "douyin_mobile_feed"
            except Exception as exc:
                diagnostics["feed_error"] = str(exc)

        # 4. Multiplatform fallback
        if not detail_data:
            from .multiplatform_video import resolve_multiplatform_video
            return await resolve_multiplatform_video(target_url)

        item_id = str(detail_data.get("aweme_id") or aweme_id or "unknown")
        title = str(detail_data.get("desc") or "Douyin Video")
        author = detail_data.get("author") or {}
        author_name = str(author.get("nickname") or author.get("unique_id") or "Douyin Creator")
        avatar_thumb = (author.get("avatar_thumb") or {}).get("url_list", [""])[0] if isinstance(author.get("avatar_thumb"), dict) else None

        # Check for image album
        images_list = detail_data.get("images") or []
        if images_list and isinstance(images_list, list):
            image_urls: list[str] = []
            for img in images_list:
                if isinstance(img, dict):
                    urls = img.get("url_list") or img.get("download_url_list") or []
                    if urls:
                        image_urls.append(urls[-1])
            if image_urls:
                return VideoResolveResult(
                    platform="douyin",
                    content_id=item_id,
                    title=title,
                    author_name=author_name,
                    author_avatar=avatar_thumb,
                    thumbnail_url=image_urls[0],
                    media_type=VideoMediaType.IMAGE_ALBUM,
                    images=image_urls,
                    diagnostics=diagnostics,
                )

        video = detail_data.get("video") or {}
        cover_url = ((video.get("cover") or {}).get("url_list", [""])[0] if isinstance(video.get("cover"), dict) else None)
        duration_ms = video.get("duration")
        duration_sec = float(duration_ms) / 1000.0 if duration_ms else None

        raw_candidates: list[dict[str, Any]] = []

        # Collect bit_rate entries
        bit_rates = video.get("bit_rate") or video.get("bitRate") or []
        if isinstance(bit_rates, list):
            for entry in bit_rates:
                if not isinstance(entry, dict):
                    continue
                gear_name = str(entry.get("gear_name") or entry.get("gearName") or "")
                bit_rate = int(entry.get("bit_rate") or entry.get("bitRate") or 0)
                fps = int(entry.get("FPS") or entry.get("fps") or 0)
                
                addr_keys = ("play_addr_265", "play_addr_bytevc1", "play_addr_h264", "play_addr", "download_addr")
                for ak in addr_keys:
                    addr = entry.get(ak)
                    if isinstance(addr, dict):
                        urls = addr.get("url_list") or []
                        if urls:
                            w = int(addr.get("width") or video.get("width") or 0)
                            h = int(addr.get("height") or video.get("height") or 0)
                            calc_w, calc_h = infer_dimensions(w, h, f"{gear_name} {ak}")
                            raw_candidates.append({
                                "url": urls[0].replace("playwm", "play"),
                                "width": calc_w,
                                "height": calc_h,
                                "fps": fps,
                                "bitrate": bit_rate,
                                "gear_name": gear_name,
                                "source": ak
                            })


        # Collect top-level video addresses
        for ak in ("play_addr_265", "play_addr_bytevc1", "play_addr_h264", "play_addr", "download_addr"):
            addr = video.get(ak)
            if isinstance(addr, dict):
                urls = addr.get("url_list") or []
                if urls:
                    w = _safe_int(addr.get("width") or video.get("width"))
                    h = _safe_int(addr.get("height") or video.get("height"))
                    raw_candidates.append({
                        "url": urls[0].replace("playwm", "play"),
                        "width": w,
                        "height": h,
                        "fps": _safe_int(video.get("fps") or video.get("FPS")),
                        "bitrate": 0,
                        "gear_name": ak,
                        "source": ak
                    })

        # Deduplicate candidates
        renditions: list[VideoRendition] = []
        seen_res: set[str] = set()

        # Sort by resolution area and bitrate descending
        raw_candidates.sort(key=lambda c: (c["width"] * c["height"], c["bitrate"], c["fps"]), reverse=True)

        for idx, c in enumerate(raw_candidates):
            w = c["width"]
            h = c["height"]
            fps = c["fps"]
            gear = c["gear_name"].lower()
            key = f"{w}x{h}"
            if key in seen_res:
                continue
            seen_res.add(key)

            min_dim = min(w, h) if (w and h) else 0
            max_dim = max(w, h) if (w and h) else 0
            fps_str = f" {fps}fps" if fps >= 50 else ""

            if max_dim >= 3840 or min_dim >= 2160 or "4k" in gear or "4_" in gear or "2160" in gear:
                label = f"4K Siêu Nét ({w}x{h}{fps_str} Gốc)"
                is_orig = True
            elif max_dim >= 2560 or min_dim >= 1440 or "2k" in gear or "1440" in gear:
                label = f"2K Siêu Nét ({w}x{h}{fps_str} Gốc)"
                is_orig = True
            elif max_dim >= 1920 or min_dim >= 1080 or "1080" in gear:
                label = f"1080p Full HD ({w}x{h}{fps_str})"
                is_orig = idx == 0
            elif max_dim >= 1280 or min_dim >= 720 or "720" in gear:
                label = f"720p HD ({w}x{h}{fps_str})"
                is_orig = idx == 0
            else:
                label = f"SD ({w}x{h})" if w and h else f"Chất lượng {idx + 1}"
                is_orig = False

            renditions.append(
                VideoRendition(
                    id=f"douyin_{item_id}_{idx}",
                    label=label,
                    url=c["url"],
                    width=w,
                    height=h,
                    fps=float(fps) if fps else None,
                    bitrate=c["bitrate"] if c["bitrate"] > 0 else None,
                    format="mp4",
                    is_original=is_orig,
                    recommended=idx == 0,
                    headers={
                        "User-Agent": DEFAULT_HEADERS["User-Agent"],
                        "Referer": "https://www.douyin.com/",
                    }
                )
            )

        if not renditions and video.get("play_addr"):
            play_urls = video["play_addr"].get("url_list") or []
            if play_urls:
                renditions.append(
                    VideoRendition(
                        id=f"douyin_{item_id}_fallback",
                        label="Chất lượng gốc HD (MP4)",
                        url=play_urls[0].replace("playwm", "play"),
                        format="mp4",
                        is_original=True,
                        recommended=True,
                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.douyin.com/"}
                    )
                )

        return VideoResolveResult(
            platform="douyin",
            content_id=item_id,
            title=title,
            author_name=author_name,
            author_avatar=avatar_thumb,
            thumbnail_url=cover_url,
            duration=duration_sec,
            media_type=VideoMediaType.VIDEO,
            renditions=renditions,
            diagnostics=diagnostics,
        )
