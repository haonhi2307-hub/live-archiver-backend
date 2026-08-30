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
    installation_id: str | None = None
    lease_id: str | None = None
    mode: str = "inspect"  # "inspect" (preview) or "record" (under active lease)


class AuthRegisterInstallationRequest(BaseModel):
    installation_id: str
    device_fingerprint: str
    public_key_pem: str


class AuthHandshakeRequest(BaseModel):
    device_fingerprint: str
    installation_id: str | None = None
    public_key_pem: str | None = None


class AuthActivateRequest(BaseModel):
    device_fingerprint: str
    key_code: str
    installation_id: str | None = None
    signature: str | None = None
    nonce: str | None = None
    timestamp_utc: str | None = None


class AuthResponse(BaseModel):
    ok: bool
    status: str
    plan: str
    days_remaining: int
    expires_at: str | None = None
    device_fingerprint: str
    installation_id: str | None = None
    server_time_utc: str
    message: str


class LeaseStartRequest(BaseModel):
    installation_id: str
    device_fingerprint: str
    session_id: str
    platform: str
    canonical_url: str
    signature: str | None = None
    nonce: str | None = None
    timestamp_utc: str | None = None


class LeaseEndRequest(BaseModel):
    lease_id: str
    installation_id: str
    session_id: str


class LeaseResponse(BaseModel):
    ok: bool
    lease_id: str | None = None
    session_id: str | None = None
    expires_at_utc: str | None = None
    status: str
    message: str
