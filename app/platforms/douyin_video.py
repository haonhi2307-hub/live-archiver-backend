from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from ..models_video import VideoMediaType, VideoRendition, VideoResolveResult


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
AWEME_ID_RE = re.compile(r"/(?:video|note|share/video)/(\d+)", re.IGNORECASE)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _clean_url(raw: str) -> str:
    match = URL_RE.search(raw)
    return match.group(0).rstrip(".,;，。；!！?？)]}>") if match else raw.strip()


async def resolve_douyin_video(raw_input: str) -> VideoResolveResult:
    target_url = _clean_url(raw_input)
    diagnostics: dict[str, Any] = {"input_url": target_url}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=12.0) as client:
        # Follow redirects for short links (e.g. v.douyin.com/...)
        resp = await client.get(target_url)
        final_url = str(resp.url)
        diagnostics["final_url"] = final_url

        aweme_id_match = AWEME_ID_RE.search(final_url) or AWEME_ID_RE.search(target_url)
        aweme_id = aweme_id_match.group(1) if aweme_id_match else ""

        # Fetch detail API
        detail_data: dict[str, Any] = {}
        if aweme_id:
            api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&aid=6383&version_code=190200&app_name=douyin_web&device_platform=webapp"
            try:
                api_resp = await client.get(api_url)
                if api_resp.status_code == 200:
                    detail_data = (api_resp.json().get("aweme_detail") or {})
            except Exception as exc:
                diagnostics["api_error"] = str(exc)

        # Fallback to HTML scrape if API is guarded
        if not detail_data and resp.text:
            text = resp.text
            # Look for render data / router data
            m = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', text)
            if m:
                try:
                    import urllib.parse
                    decoded = urllib.parse.unquote(m.group(1))
                    data = json.loads(decoded)
                    # Traverse keys to find aweme_detail
                    for k, v in data.items():
                        if isinstance(v, dict) and "awemeDetail" in v:
                            detail_data = v["awemeDetail"]
                            break
                except Exception:
                    pass

        if not detail_data:
            # Fallback to multiplatform extractor (yt-dlp)
            try:
                from .multiplatform_video import resolve_multiplatform_video
                return await resolve_multiplatform_video(target_url)
            except Exception as fb_exc:
                raise ValueError(f"Không thể bóc tách dữ liệu video Douyin từ link: {target_url} ({fb_exc})")

        item_id = str(detail_data.get("aweme_id") or aweme_id or "unknown")
        title = detail_data.get("desc") or "Douyin Video"
        author = detail_data.get("author") or {}
        author_name = author.get("nickname") or author.get("unique_id") or "Douyin Creator"
        avatar_thumb = (author.get("avatar_thumb") or {}).get("url_list", [""])[0] if isinstance(author.get("avatar_thumb"), dict) else None

        # Check if it is an image album (slideshow/note)
        images_list = detail_data.get("images") or []
        if images_list and isinstance(images_list, list):
            image_urls: list[str] = []
            for img in images_list:
                if isinstance(img, dict):
                    urls = (img.get("url_list") or img.get("download_url_list") or [])
                    if urls:
                        image_urls.append(urls[-1])  # Usually last URL is highest resolution
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

        # Video parsing
        video = detail_data.get("video") or {}
        cover_url = ((video.get("cover") or {}).get("url_list", [""])[0]) if isinstance(video.get("cover"), dict) else None
        duration = float(detail_data.get("duration", 0)) / 1000.0 if detail_data.get("duration") else None

        renditions: list[VideoRendition] = []
        bit_rates = video.get("bit_rate") or []
        seen_urls: set[str] = set()

        if isinstance(bit_rates, list) and bit_rates:
            for idx, br in enumerate(bit_rates):
                if not isinstance(br, dict):
                    continue
                play_addr = br.get("play_addr") or br.get("play_addr_265") or br.get("play_addr_h264")
                if not isinstance(play_addr, dict):
                    continue
                urls = play_addr.get("url_list") or []
                if not urls:
                    continue
                v_url = urls[0]
                if v_url in seen_urls:
                    continue
                seen_urls.add(v_url)

                w = int(play_addr.get("width") or video.get("width") or 0)
                h = int(play_addr.get("height") or video.get("height") or 0)
                gear_name = str(br.get("gear_name") or "")
                quality_type = str(br.get("quality_type") or "")
                bitrate = int(br.get("bit_rate") or 0)
                fps = float(br.get("fps") or 30.0)
                codec = "hevc" if "265" in gear_name.lower() or "bytevc1" in str(br).lower() else "h264"

                # Label generation (detect 4K if gear_name contains '4_')
                is_4k = "4_" in gear_name or w >= 2160 or h >= 2160
                if is_4k:
                    label = f"4K 2160p ({codec.upper()})"
                elif w >= 1080 or h >= 1080:
                    label = f"1080p Full HD ({codec.upper()})"
                elif w >= 720 or h >= 720:
                    label = f"720p HD ({codec.upper()})"
                else:
                    label = f"{min(w, h)}p ({codec.upper()})" if w and h else f"Chất lượng {idx + 1}"

                renditions.append(VideoRendition(
                    id=f"douyin_{item_id}_{idx}",
                    label=label,
                    url=v_url,
                    width=w,
                    height=h,
                    fps=fps,
                    bitrate=bitrate,
                    codec=codec,
                    format="mp4",
                    size_bytes=int(play_addr.get("data_size") or 0),
                    headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.douyin.com/"},
                    is_original=is_4k or (idx == 0),
                ))

        # Fallback if bit_rate array is empty
        if not renditions:
            play_addr = video.get("play_addr") or video.get("play_addr_h264")
            if isinstance(play_addr, dict):
                urls = play_addr.get("url_list") or []
                if urls:
                    w = int(video.get("width") or 1080)
                    h = int(video.get("height") or 1920)
                    renditions.append(VideoRendition(
                        id=f"douyin_{item_id}_default",
                        label="Chất lượng gốc HD (MP4)",
                        url=urls[0],
                        width=w,
                        height=h,
                        format="mp4",
                        headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.douyin.com/"},
                        is_original=True,
                        recommended=True,
                    ))

        # Sort renditions by resolution and bitrate
        renditions.sort(key=lambda r: ((r.width or 0) * (r.height or 0), r.bitrate or 0), reverse=True)
        if renditions:
            renditions[0].recommended = True

        return VideoResolveResult(
            platform="douyin",
            content_id=item_id,
            title=title,
            author_name=author_name,
            author_avatar=avatar_thumb,
            thumbnail_url=cover_url,
            duration_seconds=duration,
            media_type=VideoMediaType.VIDEO,
            renditions=renditions,
            diagnostics=diagnostics,
        )
