from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class VideoMediaType(str, Enum):
    VIDEO = "video"
    IMAGE_ALBUM = "image_album"


class VideoRendition(BaseModel):
    id: str
    label: str
    url: str
    audio_url: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    codec: str | None = None
    format: str = "mp4"
    size_bytes: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    is_original: bool = False
    recommended: bool = False


class VideoResolveRequest(BaseModel):
    url: str
    quality_preference: str = "best"  # "best", "mp4", "2160", "1080", "720", "audio"


class VideoResolveResult(BaseModel):
    platform: str
    content_id: str
    title: str = ""
    author_name: str = ""
    author_avatar: str | None = None
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    media_type: VideoMediaType = VideoMediaType.VIDEO
    renditions: list[VideoRendition] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
