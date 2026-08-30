from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from ..models_video import VideoMediaType, VideoRendition, VideoResolveResult


def _detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    if "instagram.com" in host:
        return "instagram"
    if "bilibili.com" in host:
        return "bilibili"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    return "multiplatform"


def _extract_ytdlp(url: str) -> dict[str, Any]:
    # Strategy 1: Standard comprehensive formats (up to 4K/8K)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "format": "bestvideo+bestaudio/best",
        "geo_bypass": True,
        "geo_bypass_country": "VN",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(url, download=False)
            if res and res.get("formats"):
                return res
    except Exception:
        pass

    # Strategy 2: Innertube Android / TV Client (bypasses region locks & datacenter blocks 100%)
    ydl_opts_fallback = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "android_embedded", "tv_embedded", "mweb"],
                "player_skip": ["webpage", "configs", "js"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/19.29.37 (Linux; U; Android 14; vi_VN; Pixel 8 Pro)",
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts_fallback) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def resolve_multiplatform_video(url: str) -> VideoResolveResult:
    info = await asyncio.to_thread(_extract_ytdlp, url)
    if not info:
        raise ValueError(f"Không thể lấy thông tin media từ link: {url}")

    platform = _detect_platform(url)
    content_id = str(info.get("id") or "unknown")
    title = info.get("title") or "Video"
    author_name = info.get("uploader") or info.get("channel") or info.get("creator") or "Creator"
    thumbnail_url = info.get("thumbnail")
    duration = float(info.get("duration", 0)) if info.get("duration") else None

    formats = info.get("formats") or []
    renditions: list[VideoRendition] = []

    # Find best standalone audio stream (m4a or mp3)
    audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none" and f.get("url")]
    best_audio = max(audio_formats, key=lambda f: f.get("abr") or 0, default=None)
    best_audio_url = best_audio.get("url") if best_audio else None

    # Filter video formats
    video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("url")]
    # Group by resolution height (e.g. 2160, 1440, 1080, 720, 480)
    seen_heights: set[int] = set()

    # Sort video formats by height, fps, tbr
    video_formats.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)

    for f in video_formats:
        h = f.get("height") or 0
        w = f.get("width") or 0
        if not h:
            continue
        # Take the best rendition for each standard height tier
        height_tier = h
        if height_tier in seen_heights:
            continue
        seen_heights.add(height_tier)

        fps = float(f.get("fps") or 30.0)
        vcodec = (f.get("vcodec") or "mp4").split(".")[0]
        bitrate = int((f.get("tbr") or 0) * 1000)
        v_url = f.get("url")
        # If this format has no audio, attach the best audio URL so client can remux
        has_audio = f.get("acodec") != "none"
        attached_audio = None if has_audio else best_audio_url

        if h >= 2160:
            label = f"4K 2160p ({fps:g}fps)"
        elif h >= 1440:
            label = f"2K 1440p ({fps:g}fps)"
        elif h >= 1080:
            label = f"1080p Full HD ({fps:g}fps)"
        elif h >= 720:
            label = f"720p HD ({fps:g}fps)"
        else:
            label = f"{h}p ({fps:g}fps)"

        renditions.append(VideoRendition(
            id=f"{platform}_{content_id}_{h}p",
            label=label,
            url=v_url,
            audio_url=attached_audio,
            width=w,
            height=h,
            fps=fps,
            bitrate=bitrate,
            codec=vcodec,
            format="mp4",
            headers=f.get("http_headers") or {},
            is_original=(h >= 1080),
        ))

    # Add audio-only rendition if available
    if best_audio_url:
        renditions.append(VideoRendition(
            id=f"{platform}_{content_id}_audio",
            label="Chỉ Âm Thanh (MP3 / M4A)",
            url=best_audio_url,
            codec="aac",
            format="m4a",
            headers=best_audio.get("http_headers") or {},
            is_original=False,
        ))

    if renditions:
        renditions[0].recommended = True

    return VideoResolveResult(
        platform=platform,
        content_id=content_id,
        title=title,
        author_name=author_name,
        thumbnail_url=thumbnail_url,
        duration_seconds=duration,
        media_type=VideoMediaType.VIDEO,
        renditions=renditions,
        diagnostics={"format_count": len(formats)},
    )
