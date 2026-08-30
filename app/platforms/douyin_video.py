from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..models_video import VideoMediaType, VideoRendition, VideoResolveResult


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
AWEME_ID_RE = re.compile(r"/(?:video|note|share/video)/(\d+)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b(\d{18,20})\b")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _clean_url(raw: str) -> str:
    match = URL_RE.search(raw)
    return match.group(0).rstrip(".,;，。；!！?？)]}>") if match else raw.strip()


async def _get_ttwid(client: httpx.AsyncClient) -> str:
    """Register official ByteDance ttwid session for unlocking 4K/60fps master streams."""
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
        ttwid = resp.cookies.get("ttwid")
        if ttwid:
            return ttwid
    except Exception:
        pass
    return ""


async def resolve_douyin_video(raw_input: str) -> VideoResolveResult:
    target_url = _clean_url(raw_input)
    diagnostics: dict[str, Any] = {"input_url": target_url}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
        resp = await client.get(target_url)
        final_url = str(resp.url)
        diagnostics["final_url"] = final_url

        aweme_id_match = AWEME_ID_RE.search(final_url) or AWEME_ID_RE.search(target_url) or NUMBER_RE.search(final_url) or NUMBER_RE.search(target_url)
        aweme_id = aweme_id_match.group(1) if aweme_id_match else ""

        detail_data: dict[str, Any] = {}

        # TIER 1: Official Douyin Web Detail API with ByteDance ttwid (Unlocks True 4K 60fps & 2K 60fps)
        if aweme_id:
            try:
                ttwid = await _get_ttwid(client)
                headers = dict(DEFAULT_HEADERS)
                if ttwid:
                    headers["Cookie"] = f"ttwid={ttwid};"

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
                    "screen_width": "2560",
                    "screen_height": "1440",
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
                        diagnostics["source"] = "douyin_ttwid_web_detail_4k"
            except Exception as exc:
                diagnostics["ttwid_web_error"] = str(exc)

        # TIER 2: ByteDance Mobile Feed API (Fast Fallback)
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
                diagnostics["feed_api_error"] = str(exc)

        # TIER 3: Multiplatform fallback (yt-dlp)
        if not detail_data:
            try:
                from .multiplatform_video import resolve_multiplatform_video
                return await resolve_multiplatform_video(target_url)
            except Exception as fb_exc:
                raise ValueError(f"Không thể bóc tách dữ liệu video Douyin từ link: {target_url} ({fb_exc})")

        item_id = str(detail_data.get("aweme_id") or aweme_id or "unknown")
        title = str(detail_data.get("desc") or "Douyin Video")
        author = detail_data.get("author") or {}
        author_name = str(author.get("nickname") or author.get("unique_id") or "Douyin Creator")
        avatar_thumb = (author.get("avatar_thumb") or {}).get("url_list", [""])[0] if isinstance(author.get("avatar_thumb"), dict) else None

        # Check for image album / slideshow
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
        cover_url = ((video.get("cover") or {}).get("url_list", [""])[0] if isinstance(video.get("cover"), dict) else None) or ((video.get("origin_cover") or {}).get("url_list", [""])[0] if isinstance(video.get("origin_cover"), dict) else None)
        duration_ms = video.get("duration")
        duration_sec = float(duration_ms) / 1000.0 if duration_ms else None

        renditions: list[VideoRendition] = []
        seen_keys: set[str] = set()

        bitrate_list = video.get("bit_rate") or []
        if isinstance(bitrate_list, list) and bitrate_list:
            for idx, item in enumerate(bitrate_list):
                if not isinstance(item, dict):
                    continue
                play_addr = item.get("play_addr") or {}
                urls = play_addr.get("url_list") or []
                if not urls:
                    continue
                best_url = urls[0].replace("playwm", "play")

                gear_name = str(item.get("gear_name") or "").lower()
                bit_rate = int(item.get("bit_rate") or 0)
                fps = int(item.get("FPS") or item.get("fps") or 0)
                w = int(play_addr.get("width") or video.get("width") or 0)
                h = int(play_addr.get("height") or video.get("height") or 0)
                size_bytes = int(play_addr.get("data_size") or 0)

                key = f"{w}x{h}_{fps}_{bit_rate}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                min_dim = min(w, h) if (w and h) else 0
                is_4k = "4_" in gear_name or "2160" in gear_name or min_dim >= 2160
                is_2k = "1440" in gear_name or min_dim >= 1440
                is_1080p = "1080" in gear_name or min_dim >= 1080

                fps_str = f" {fps}fps" if fps and fps >= 50 else ""

                if is_4k:
                    label = f"4K Siêu Nét (2160p{fps_str} Gốc)"
                elif is_2k:
                    label = f"2K Siêu Nét (1440p{fps_str} Gốc)"
                elif is_1080p:
                    label = f"1080p Full HD ({w}x{h}{fps_str})"
                elif min_dim >= 720:
                    label = f"720p HD ({w}x{h}{fps_str})"
                else:
                    label = f"SD ({w}x{h})" if w and h else f"Chất lượng {idx + 1}"

                renditions.append(
                    VideoRendition(
                        id=f"douyin_{item_id}_{idx}",
                        label=label,
                        url=best_url,
                        width=w,
                        height=h,
                        bitrate=bit_rate,
                        fps=float(fps) if fps else None,
                        codec="hevc" if "265" in gear_name or "bytevc1" in str(item).lower() else "h264",
                        format="mp4",
                        size_bytes=size_bytes,
                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.douyin.com/"},
                        is_original=is_4k or (idx == 0),
                    )
                )

        if not renditions:
            play_addr = video.get("play_addr") or {}
            urls = play_addr.get("url_list") or []
            if urls:
                best_url = urls[0].replace("playwm", "play")
                renditions.append(
                    VideoRendition(
                        id=f"douyin_{item_id}_main",
                        label="1080p Full HD (Gốc Không Logo)",
                        url=best_url,
                        width=int(video.get("width") or 1080),
                        height=int(video.get("height") or 1920),
                        format="mp4",
                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.douyin.com/"},
                        is_original=True,
                        recommended=True,
                    )
                )

        # Sort renditions by resolution (area) and bitrate descending
        renditions.sort(
            key=lambda r: (
                1 if "4K" in r.label else (0.5 if "2K" in r.label else 0),
                (r.width or 0) * (r.height or 0),
                r.bitrate or 0,
                r.size_bytes or 0,
            ),
            reverse=True,
        )
        if renditions:
            renditions[0].recommended = True
            renditions[0].is_original = True

        return VideoResolveResult(
            platform="douyin",
            content_id=item_id,
            title=title,
            author_name=author_name,
            author_avatar=avatar_thumb,
            thumbnail_url=cover_url,
            duration_seconds=duration_sec,
            media_type=VideoMediaType.VIDEO,
            renditions=renditions,
            diagnostics=diagnostics,
        )
