from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..models_video import VideoMediaType, VideoRendition, VideoResolveResult


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TIKTOK_ID_RE = re.compile(r"/(?:video|photo)/(\d+)", re.IGNORECASE)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.tiktok.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}


def _clean_url(raw: str) -> str:
    match = URL_RE.search(raw)
    return match.group(0).rstrip(".,;，。；!！?？)]}>") if match else raw.strip()


async def resolve_tiktok_video(raw_input: str) -> VideoResolveResult:
    target_url = _clean_url(raw_input)
    diagnostics: dict[str, Any] = {"input_url": target_url}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=12.0) as client:
        resp = await client.get(target_url)
        final_url = str(resp.url)
        diagnostics["final_url"] = final_url

        video_id_match = TIKTOK_ID_RE.search(final_url) or TIKTOK_ID_RE.search(target_url)
        video_id = video_id_match.group(1) if video_id_match else ""

        item_struct: dict[str, Any] = {}

        # Strategy 1: Universal Data / Rehydration from HTML
        if resp.text:
            text = resp.text
            m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">([^<]+)</script>', text)
            if m:
                try:
                    data = json.loads(m.group(1))
                    default_scope = (data.get("__DEFAULT_SCOPE__") or {})
                    detail = default_scope.get("webapp.video-detail") or {}
                    item_struct = detail.get("itemInfo", {}).get("itemStruct") or {}
                except Exception as exc:
                    diagnostics["rehydration_error"] = str(exc)

            # Fallback to SIGI_STATE
            if not item_struct:
                m = re.search(r'<script id="SIGI_STATE" type="application/json">([^<]+)</script>', text)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        item_module = data.get("ItemModule") or {}
                        if video_id and video_id in item_module:
                            item_struct = item_module[video_id]
                        elif item_module:
                            item_struct = next(iter(item_module.values()))
                    except Exception as exc:
                        diagnostics["sigi_error"] = str(exc)

        # Strategy 2: Mobile Feed API
        if not item_struct and video_id:
            try:
                api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}&version_code=2613&app_name=musical_ly&channel=App%20Store&device_id=1234567890&device_platform=iphone"
                api_resp = await client.get(api_url, headers={"User-Agent": "TikTok 26.1.3 rv:261310 (iPhone; iOS 16.0; en_US)"})
                if api_resp.status_code == 200:
                    aweme_list = api_resp.json().get("aweme_list") or []
                    if aweme_list:
                        item_struct = aweme_list[0]
            except Exception as exc:
                diagnostics["feed_api_error"] = str(exc)

        if not item_struct:
            # Fallback to multiplatform extractor (yt-dlp)
            try:
                from .multiplatform_video import resolve_multiplatform_video
                return await resolve_multiplatform_video(target_url)
            except Exception as fb_exc:
                raise ValueError(f"Không thể lấy thông tin video TikTok từ link: {target_url} ({fb_exc})")

        item_id = str(item_struct.get("id") or item_struct.get("aweme_id") or video_id or "unknown")
        title = item_struct.get("desc") or "TikTok Video"
        author = item_struct.get("author") or {}
        author_name = author.get("nickname") or author.get("uniqueId") or author.get("unique_id") or "TikTok Creator"
        author_avatar = (
            (author.get("avatarThumb") if isinstance(author.get("avatarThumb"), str) else None)
            or ((author.get("avatar_thumb") or {}).get("url_list", [""])[0] if isinstance(author.get("avatar_thumb"), dict) else None)
        )

        # Check for image album (photo post)
        image_post_info = item_struct.get("imagePostInfo") or item_struct.get("image_post_info")
        if image_post_info and isinstance(image_post_info, dict):
            images_list = image_post_info.get("images") or []
            image_urls: list[str] = []
            for img in images_list:
                if isinstance(img, dict):
                    # display_image or image_url
                    display_img = img.get("displayImage") or img.get("display_image") or img.get("imageURL") or img.get("image_url") or {}
                    urls = display_img.get("urlList") or display_img.get("url_list") or []
                    if urls:
                        image_urls.append(urls[-1])
            if image_urls:
                return VideoResolveResult(
                    platform="tiktok",
                    content_id=item_id,
                    title=title,
                    author_name=author_name,
                    author_avatar=author_avatar,
                    thumbnail_url=image_urls[0],
                    media_type=VideoMediaType.IMAGE_ALBUM,
                    images=image_urls,
                    diagnostics=diagnostics,
                )

        # Video parsing
        video = item_struct.get("video") or {}
        cover_url = video.get("cover") if isinstance(video.get("cover"), str) else ((video.get("cover") or {}).get("url_list", [""])[0] if isinstance(video.get("cover"), dict) else None)
        duration = float(video.get("duration", 0)) if video.get("duration") else None

        renditions: list[VideoRendition] = []
        bitrate_info = video.get("bitrateInfo") or video.get("bit_rate") or []
        seen_urls: set[str] = set()

        if isinstance(bitrate_info, list) and bitrate_info:
            for idx, br in enumerate(bitrate_info):
                if not isinstance(br, dict):
                    continue
                play_addr = br.get("PlayAddr") or br.get("play_addr") or br.get("playAddr") or {}
                urls = play_addr.get("UrlList") or play_addr.get("url_list") or []
                if not urls:
                    continue
                v_url = urls[0]
                if v_url in seen_urls:
                    continue
                seen_urls.add(v_url)

                w = int(play_addr.get("Width") or play_addr.get("width") or video.get("width") or 0)
                h = int(play_addr.get("Height") or play_addr.get("height") or video.get("height") or 0)
                bitrate = int(br.get("Bitrate") or br.get("bit_rate") or 0)
                gear_name = str(br.get("GearName") or br.get("gear_name") or br.get("quality_type") or "")
                codec = "hevc" if "265" in gear_name.lower() or "bytevc1" in str(br).lower() else "h264"

                if w >= 1080 or h >= 1080:
                    label = f"1080p Full HD ({codec.upper()})"
                elif w >= 720 or h >= 720:
                    label = f"720p HD ({codec.upper()})"
                else:
                    label = f"{min(w, h)}p ({codec.upper()})" if w and h else f"Chất lượng {idx + 1}"

                renditions.append(VideoRendition(
                    id=f"tiktok_{item_id}_{idx}",
                    label=label,
                    url=v_url,
                    width=w,
                    height=h,
                    bitrate=bitrate,
                    codec=codec,
                    format="mp4",
                    size_bytes=int(play_addr.get("DataSize") or play_addr.get("data_size") or 0),
                    headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.tiktok.com/"},
                    is_original=(idx == 0),
                ))

        # Fallback to playAddr / downloadAddr
        if not renditions:
            play_url = video.get("playAddr") or video.get("downloadAddr")
            if isinstance(play_url, str) and play_url:
                renditions.append(VideoRendition(
                    id=f"tiktok_{item_id}_main",
                    label="Gốc Không Logo (No Watermark HD)",
                    url=play_url,
                    width=int(video.get("width") or 1080),
                    height=int(video.get("height") or 1920),
                    format="mp4",
                    headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.tiktok.com/"},
                    is_original=True,
                    recommended=True,
                ))

        renditions.sort(key=lambda r: ((r.width or 0) * (r.height or 0), r.bitrate or 0), reverse=True)
        if renditions:
            renditions[0].recommended = True

        return VideoResolveResult(
            platform="tiktok",
            content_id=item_id,
            title=title,
            author_name=author_name,
            author_avatar=author_avatar,
            thumbnail_url=cover_url,
            duration_seconds=duration,
            media_type=VideoMediaType.VIDEO,
            renditions=renditions,
            diagnostics=diagnostics,
        )
