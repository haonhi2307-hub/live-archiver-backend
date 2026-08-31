from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1/app", tags=["App Updater"])

UPDATES_DIR = Path(__file__).resolve().parent / "updates"
MANIFEST_PATH = UPDATES_DIR / "manifest.json"

class UpdateCheckRequest(BaseModel):
    current_version_code: int
    rollout_id: Optional[str] = None
    channel: Optional[str] = "stable"

class AppUpdateManifest(BaseModel):
    package_name: str = "com.hao.livearchiver"
    version_code: int
    version_name: str
    minimum_supported_version_code: int
    apk_url: str
    apk_size: int
    apk_sha256: str
    signing_cert_sha256: str
    mandatory: bool = False
    changelog: str = ""
    rollout_percentage: int = 100
    channel: str = "stable"
    published_at: str = ""

class UpdateCheckResponse(BaseModel):
    update_available: bool
    manifest: Optional[AppUpdateManifest] = None
    message: Optional[str] = None

def compute_rollout_bucket(rollout_id: str, version_code: int, channel: str) -> int:
    if not rollout_id:
        return 1
    seed = f"{rollout_id}:{version_code}:{channel}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    val = int(digest[:8], 16)
    return (val % 100) + 1

DEFAULT_MANIFEST = {
    "package_name": "com.hao.livearchiver",
    "version_code": 17,
    "version_name": "0.7.2",
    "minimum_supported_version_code": 14,
    "apk_url": "https://live-archiver-backend.onrender.com/v1/app/download/LiveArchiver_v0.7.2.apk",
    "apk_size": 21848361,
    "apk_sha256": "ccfe1c2e88d2b811195552c075a1209c3af692b3b5d516d3b16d825d8ae2e29f",
    "signing_cert_sha256": "747a30f81bc44b80f3369a991dd3cd8601d7c78a4a7ce4f6aa455bc3ab22fc24",
    "mandatory": False,
    "changelog": "• Tự động cập nhật 1 chạm (In-App Auto Updater)\n• Tích hợp Douyin 4K Native On-Device Engine\n• Nâng cấp bộ đệm tải tốc độ cao 256KB & Strict MediaRemuxer",
    "rollout_percentage": 100,
    "channel": "stable",
    "published_at": "2026-08-30T11:37:21Z"
}

import httpx

_cached_gh_apk: dict[str, Any] = {"url": None, "size": None, "tag": None, "expires_at": 0}

def get_latest_github_release_asset() -> tuple[Optional[str], Optional[int], Optional[str]]:
    import time
    now = time.time()
    if _cached_gh_apk.get("url") and now < _cached_gh_apk.get("expires_at", 0):
        return _cached_gh_apk["url"], _cached_gh_apk["size"], _cached_gh_apk["tag"]

    url = "https://api.github.com/repos/haonhi2307-hub/live-archiver-backend/releases/latest"
    try:
        r = httpx.get(url, timeout=4.0, headers={"User-Agent": "LiveArchiverBackend/1.0"})
        if r.status_code == 200:
            data = r.json()
            tag = data.get("tag_name")
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                download_url = asset.get("browser_download_url")
                size = asset.get("size")
                if name.endswith(".apk") and download_url:
                    _cached_gh_apk["url"] = download_url
                    _cached_gh_apk["size"] = size
                    _cached_gh_apk["tag"] = tag
                    _cached_gh_apk["expires_at"] = now + 60
                    return download_url, size, tag
    except Exception as e:
        print(f"Error fetching latest GitHub release: {e}")
    return _cached_gh_apk.get("url"), _cached_gh_apk.get("size"), _cached_gh_apk.get("tag")

def load_manifest() -> Optional[AppUpdateManifest]:
    manifest_obj: Optional[AppUpdateManifest] = None
    candidates = [
        MANIFEST_PATH,
        Path(__file__).resolve().parent / "manifest.json",
        Path("app/updates/manifest.json"),
        Path("app/manifest.json"),
        Path("manifest.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                manifest_obj = AppUpdateManifest(**data)
                break
            except Exception as e:
                print(f"Error loading update manifest from {p}: {e}")

    if not manifest_obj:
        try:
            manifest_obj = AppUpdateManifest(**DEFAULT_MANIFEST)
        except Exception:
            return None

    # Dynamically verify and inject exact working GitHub Release URL
    gh_url, gh_size, gh_tag = get_latest_github_release_asset()
    if gh_url:
        manifest_obj.apk_url = gh_url
        if gh_size:
            manifest_obj.apk_size = gh_size

    return manifest_obj

@router.post("/update-check", response_model=UpdateCheckResponse)
async def check_update_post(req: UpdateCheckRequest):
    manifest = load_manifest()
    if not manifest:
        return UpdateCheckResponse(update_available=False, message="Chưa có bản cập nhật nào được phát hành")

    if manifest.version_code <= req.current_version_code:
        return UpdateCheckResponse(update_available=False, message="Ứng dụng đang ở phiên bản mới nhất")

    # Staged rollout check
    rollout_id = req.rollout_id or ""
    bucket = compute_rollout_bucket(rollout_id, manifest.version_code, req.channel or "stable")
    if bucket > manifest.rollout_percentage:
        return UpdateCheckResponse(update_available=False, message="Bản cập nhật đang được phân phối theo đợt")

    return UpdateCheckResponse(update_available=True, manifest=manifest)

@router.get("/update-check", response_model=UpdateCheckResponse)
async def check_update_get(
    current_version_code: int = Query(..., alias="current_version_code"),
    rollout_id: Optional[str] = Query(None, alias="rollout_id"),
    channel: Optional[str] = Query("stable", alias="channel")
):
    req = UpdateCheckRequest(
        current_version_code=current_version_code,
        rollout_id=rollout_id,
        channel=channel
    )
    return await check_update_post(req)

@router.get("/download/{filename}")
async def download_apk(filename: str):
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".apk"):
        raise HTTPException(status_code=400, detail="Tên tệp APK không hợp lệ")

    for p in [UPDATES_DIR / safe_name, Path(f"app/updates/{safe_name}"), Path(f"updates/{safe_name}"), Path(safe_name)]:
        if p.exists():
            return FileResponse(
                path=str(p),
                media_type="application/vnd.android.package-archive",
                filename=safe_name
            )

    gh_url, _, _ = get_latest_github_release_asset()
    if gh_url:
        return RedirectResponse(url=gh_url, status_code=302)

    version_match = re.search(r"v?(\d+\.\d+\.\d+)", safe_name)
    tag = f"v{version_match.group(1)}" if version_match else "v0.7.8"
    release_url = f"https://github.com/haonhi2307-hub/live-archiver-backend/releases/download/{tag}/{safe_name}"
    return RedirectResponse(url=release_url, status_code=302)
