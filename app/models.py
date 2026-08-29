from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator


class Platform(str, Enum):
    TIKTOK = "tiktok"
    DOUYIN = "douyin"
    FACEBOOK = "facebook"


class LiveState(str, Enum):
    LIVE = "LIVE"
    OFFLINE = "OFFLINE"
    ENDED = "ENDED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    REGION_BLOCKED = "REGION_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    ANTI_BOT = "ANTI_BOT"
    PARSER_CHANGED = "PARSER_CHANGED"
    STREAM_UNAVAILABLE = "STREAM_UNAVAILABLE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class StreamCandidate(BaseModel):
    id: str
    protocol: str
    url: str
    platform_quality: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None  # bits/sec
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: int | None = None
    quality_confidence: float = 0.5

    # Quality/provenance metadata. All fields are optional for wire compatibility.
    source: str | None = None
    provenance: str | None = None
    is_original: bool = False
    verified: bool = False
    recommended: bool = False
    quality_note: str | None = None
    observed_by_player: bool = False
    derived: bool = False
    stream_family_id: str | None = None
    rendition_suffix: str | None = None
    stability_score: float | None = None
    probe_error: str | None = None
    manifest_parent: str | None = None

    @field_validator("fps", mode="before")
    @classmethod
    def sanitize_fps(cls, value):
        if value is None:
            return None
        try:
            fps = float(value)
        except (TypeError, ValueError):
            return None
        return fps if 1.0 <= fps <= 240.0 else None


class ResolveResult(BaseModel):
    platform: Platform
    state: LiveState
    canonical_url: str
    content_id: str | None = None
    creator_id: str | None = None
    creator_name: str | None = None
    title: str | None = None
    strategy: str
    streams: list[StreamCandidate] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ResolveRequest(BaseModel):
    url: str
    device_fingerprint: str | None = None


class AuthHandshakeRequest(BaseModel):
    device_fingerprint: str


class AuthActivateRequest(BaseModel):
    device_fingerprint: str
    key_code: str
