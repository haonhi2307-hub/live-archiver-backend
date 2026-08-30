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

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0) as client:
        # TIER 1: Dedicated TikWM Extractor (High reliability, No Watermark, 1080p HD)
        try:
            tw_resp = await client.post("https://www.tikwm.com/api/", data={"url": target_url, "hd": 1})
            if tw_resp.status_code == 200:
                tw_json = tw_resp.json()
                data = tw_json.get("data")
                if data and isinstance(data, dict):
                    item_id = str(data.get("id") or "tiktok_video")
                    title = str(data.get("title") or "TikTok Video")
                    author = data.get("author") or {}
                    author_name = str(author.get("nickname") or author.get("unique_id") or "TikTok Creator")
                    author_avatar = author.get("avatar")
                    cover_url = data.get("cover")
                    duration = float(data.get("duration") or 0) if data.get("duration") else None

                    # Check for image album
                    images_list = data.get("images") or []
                    if images_list and isinstance(images_list, list):
                        return VideoResolveResult(
                            platform="tiktok",
                            content_id=item_id,
                            title=title,
                            author_name=author_name,
                            author_avatar=author_avatar,
                            thumbnail_url=images_list[0],
                            media_type=VideoMediaType.IMAGE_ALBUM,
                            images=[str(img) for img in images_list],
                            diagnostics={"source": "tikwm_album"},
                        )

                    renditions: list[VideoRendition] = []
                    hd_play = data.get("hdplay")
                    normal_play = data.get("play")
                    music_url = data.get("music")

                    if hd_play and isinstance(hd_play, str):
                        renditions.append(VideoRendition(
                            id=f"tiktok_{item_id}_hd",
                            label="1080p Full HD (Gốc Không Logo)",
                            url=hd_play if hd_play.startswith("http") else f"https://www.tikwm.com{hd_play}",
                            width=1080,
                            height=1920,
                            format="mp4",
                            is_original=True,
                            recommended=True,
                        ))

                    if normal_play and isinstance(normal_play, str):
                        renditions.append(VideoRendition(
                            id=f"tiktok_{item_id}_sd",
                            label="720p HD (Không Logo)",
                            url=normal_play if normal_play.startswith("http") else f"https://www.tikwm.com{normal_play}",
                            width=720,
                            height=1280,
                            format="mp4",
                            is_original=False,
                            recommended=(len(renditions) == 0),
                        ))

                    if music_url and isinstance(music_url, str):
                        renditions.append(VideoRendition(
                            id=f"tiktok_{item_id}_audio",
                            label="Chỉ Âm Thanh Nhạc Gốc (MP3)",
                            url=music_url if music_url.startswith("http") else f"https://www.tikwm.com{music_url}",
                            codec="mp3",
                            format="mp3",
                            is_original=False,
                        ))

                    if renditions:
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
                            diagnostics={"source": "tikwm_video"},
                        )
        except Exception as exc:
            diagnostics["tikwm_error"] = str(exc)

        # TIER 2: Direct TikTok Web / Mobile API Scraper
        try:
            resp = await client.get(target_url)
            final_url = str(resp.url)
            diagnostics["final_url"] = final_url

            video_id_match = TIKTOK_ID_RE.search(final_url) or TIKTOK_ID_RE.search(target_url)
            video_id = video_id_match.group(1) if video_id_match else ""

            item_struct: dict[str, Any] = {}
            if resp.text:
                text = resp.text
                m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">([^<]+)</script>', text)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        default_scope = (data.get("__DEFAULT_SCOPE__") or {})
                        detail = default_scope.get("webapp.video-detail") or {}
                        item_struct = detail.get("itemInfo", {}).get("itemStruct") or {}
                    except Exception:
                        pass

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
                        except Exception:
                            pass

            if not item_struct and video_id:
                api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}&version_code=2613&app_name=musical_ly&channel=App%20Store&device_id=1234567890&device_platform=iphone"
                api_resp = await client.get(api_url, headers={"User-Agent": "TikTok 26.1.3 rv:261310 (iPhone; iOS 16.0; en_US)"})
                if api_resp.status_code == 200:
                    aweme_list = api_resp.json().get("aweme_list") or []
                    if aweme_list:
                        item_struct = aweme_list[0]

            if item_struct:
                item_id = str(item_struct.get("id") or item_struct.get("aweme_id") or video_id or "unknown")
                title = item_struct.get("desc") or "TikTok Video"
                author = item_struct.get("author") or {}
                author_name = author.get("nickname") or author.get("uniqueId") or author.get("unique_id") or "TikTok Creator"
                author_avatar = (
                    (author.get("avatarThumb") if isinstance(author.get("avatarThumb"), str) else None)
                    or ((author.get("avatar_thumb") or {}).get("url_list", [""])[0] if isinstance(author.get("avatar_thumb"), dict) else None)
                )

                # Check for image album
                image_post_info = item_struct.get("imagePostInfo") or item_struct.get("image_post_info")
                if image_post_info and isinstance(image_post_info, dict):
                    images_list = image_post_info.get("images") or []
                    image_urls: list[str] = []
                    for img in images_list:
                        if isinstance(img, dict):
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

                video = item_struct.get("video") or {}
                cover_url = video.get("cover") if isinstance(video.get("cover"), str) else ((video.get("cover") or {}).get("url_list", [""])[0] if isinstance(video.get("cover"), dict) else None)
                duration = float(video.get("duration", 0)) if video.get("duration") else None

                renditions = []
                bitrate_info = video.get("bitrateInfo") or video.get("bit_rate") or []
                if isinstance(bitrate_info, list) and bitrate_info:
                    for idx, br in enumerate(bitrate_info):
                        if not isinstance(br, dict):
                            continue
                        play_addr = br.get("PlayAddr") or br.get("play_addr") or br.get("playAddr") or {}
                        urls = play_addr.get("UrlList") or play_addr.get("url_list") or []
                        if not urls:
                            continue
                        w = int(play_addr.get("Width") or play_addr.get("width") or video.get("width") or 0)
                        h = int(play_addr.get("Height") or play_addr.get("height") or video.get("height") or 0)
                        bitrate = int(br.get("Bitrate") or br.get("bit_rate") or 0)
                        gear_name = str(br.get("GearName") or br.get("gear_name") or br.get("quality_type") or "")
                        codec = "hevc" if "265" in gear_name.lower() or "bytevc1" in str(br).lower() else "h264"

                        label = "1080p Full HD" if w >= 1080 or h >= 1080 else ("720p HD" if w >= 720 or h >= 720 else f"{min(w, h)}p")
                        renditions.append(VideoRendition(
                            id=f"tiktok_{item_id}_{idx}",
                            label=f"{label} ({codec.upper()})",
                            url=urls[0],
                            width=w,
                            height=h,
                            bitrate=bitrate,
                            codec=codec,
                            format="mp4",
                            headers={"User-Agent": DEFAULT_HEADERS["User-Agent"], "Referer": "https://www.tiktok.com/"},
                            is_original=(idx == 0),
                        ))

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

                if renditions:
                    renditions.sort(key=lambda r: ((r.width or 0) * (r.height or 0), r.bitrate or 0), reverse=True)
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
        except Exception as exc:
            diagnostics["native_error"] = str(exc)

        # TIER 3: Multiplatform yt-dlp fallback
        from .multiplatform_video import resolve_multiplatform_video
        return await resolve_multiplatform_video(target_url)
