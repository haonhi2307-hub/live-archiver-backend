from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
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

def load_manifest() -> Optional[AppUpdateManifest]:
    if not MANIFEST_PATH.exists():
        return None
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return AppUpdateManifest(**data)
    except Exception as e:
        print(f"Error loading update manifest: {e}")
        return None

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
    file_path = UPDATES_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp APK")
    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.android.package-archive",
        filename=safe_name
    )
