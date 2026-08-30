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
    "User-Agent": "com.ss.android.ugc.aweme/230101 (Linux; U; Android 14; zh_CN; 23116PN5BC)",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
}


def _clean_url(raw: str) -> str:
    match = URL_RE.search(raw)
    return match.group(0).rstrip(".,;，。；!！?？)]}>") if match else raw.strip()


async def resolve_douyin_video(raw_input: str) -> VideoResolveResult:
    target_url = _clean_url(raw_input)
    diagnostics: dict[str, Any] = {"input_url": target_url}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
        # Follow redirects for short links (e.g. v.douyin.com/...)
        resp = await client.get(target_url)
        final_url = str(resp.url)
        diagnostics["final_url"] = final_url

        aweme_id_match = AWEME_ID_RE.search(final_url) or AWEME_ID_RE.search(target_url) or NUMBER_RE.search(final_url) or NUMBER_RE.search(target_url)
        aweme_id = aweme_id_match.group(1) if aweme_id_match else ""

        detail_data: dict[str, Any] = {}

        # TIER 1: Official ByteDance Mobile Feed API (Instant, No-Cookie, Full HD/4K)
        if aweme_id:
            try:
                feed_url = f"https://api3-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id={aweme_id}&device_platform=android&version_code=230101"
                api_resp = await client.get(feed_url)
                if api_resp.status_code == 200:
                    aweme_list = api_resp.json().get("aweme_list") or []
                    if aweme_list:
                        detail_data = aweme_list[0]
                        diagnostics["source"] = "douyin_mobile_feed"
            except Exception as exc:
                diagnostics["feed_api_error"] = str(exc)

        # TIER 2: Douyin Web Detail API
        if not detail_data and aweme_id:
            try:
                web_api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&aid=6383&version_code=190200&app_name=douyin_web&device_platform=webapp"
                web_resp = await client.get(web_api_url, headers={"Referer": "https://www.douyin.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if web_resp.status_code == 200:
                    detail_data = web_resp.json().get("aweme_detail") or {}
                    diagnostics["source"] = "douyin_web_detail"
            except Exception as exc:
                diagnostics["web_api_error"] = str(exc)

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

        # Check if it is an image album (slideshow/note)
        images_list = detail_data.get("images") or []
        if images_list and isinstance(images_list, list):
            image_urls: list[str] = []
            for img in images_list:
                if isinstance(img, dict):
                    urls = (img.get("url_list") or img.get("download_url_list") or [])
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
        bitrate_list = video.get("bit_rate") or []
        seen_urls: set[str] = set()

        if isinstance(bitrate_list, list) and bitrate_list:
            for idx, item in enumerate(bitrate_list):
                if not isinstance(item, dict):
                    continue
                play_addr = item.get("play_addr") or {}
                urls = play_addr.get("url_list") or []
                if not urls:
                    continue
                best_url = urls[0].replace("playwm", "play")  # Ensure watermark-free
                if best_url in seen_urls:
                    continue
                seen_urls.add(best_url)

                gear_name = str(item.get("gear_name") or "").lower()
                quality_type = int(item.get("quality_type") or 0)
                bit_rate = int(item.get("bit_rate") or 0)
                w = int(play_addr.get("width") or video.get("width") or 0)
                h = int(play_addr.get("height") or video.get("height") or 0)

                is_4k = gear_name.startswith("4_") or "4k" in gear_name or "2160" in gear_name or w >= 3840 or h >= 3840
                is_1080p = "1080" in gear_name or quality_type in (1, 2) or w >= 1080 or h >= 1080

                if is_4k:
                    label = "4K Siêu Nét (2160p HEVC)"
                elif is_1080p:
                    label = "1080p Full HD (Chất Lượng Gốc)"
                elif w >= 720 or h >= 720:
                    label = "720p HD"
                else:
                    label = f"{min(w, h)}p" if w and h else f"Chất lượng {idx + 1}"

                renditions.append(
                    VideoRendition(
                        id=f"douyin_{item_id}_{idx}",
                        label=label,
                        url=best_url,
                        width=w,
                        height=h,
                        bitrate=bit_rate,
                        codec="hevc" if "265" in gear_name or "bytevc1" in str(item).lower() else "h264",
                        format="mp4",
                        size_bytes=int(play_addr.get("data_size") or 0),
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

        renditions.sort(
            key=lambda r: (1 if "4K" in r.label else 0, (r.width or 0) * (r.height or 0), r.bitrate or 0),
            reverse=True,
        )
        if renditions:
            renditions[0].recommended = True

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
