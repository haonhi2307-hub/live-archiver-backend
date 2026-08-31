from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import string
from typing import Any

from .settings import settings

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.exceptions import InvalidSignature
except ImportError:
    ec = None
    hashes = None
    serialization = None
    InvalidSignature = Exception

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "license.db"


def _now_utc() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_installations (
                installation_id TEXT PRIMARY KEY,
                device_fingerprint TEXT NOT NULL,
                public_key_pem TEXT,
                first_seen_at_utc INTEGER NOT NULL,
                trial_expires_at_utc INTEGER NOT NULL,
                active_license_key TEXT,
                status TEXT NOT NULL DEFAULT 'TRIAL',
                created_at_utc INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_inst_fingerprint ON device_installations(device_fingerprint);

            CREATE TABLE IF NOT EXISTS device_trials (
                device_fingerprint TEXT PRIMARY KEY,
                first_seen_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS licenses (
                key_code TEXT PRIMARY KEY,
                duration_days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNACTIVATED',
                bound_device TEXT,
                bound_installation_id TEXT,
                activated_at INTEGER,
                expires_at INTEGER,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_licenses_bound ON licenses(bound_device);

            CREATE TABLE IF NOT EXISTS recording_leases (
                lease_id TEXT PRIMARY KEY,
                installation_id TEXT NOT NULL,
                device_fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                issued_at_utc INTEGER NOT NULL,
                expires_at_utc INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at_utc INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lease_lookup ON recording_leases(lease_id, status);

            CREATE TABLE IF NOT EXISTS admin_sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                created_at_utc INTEGER NOT NULL,
                expires_at_utc INTEGER NOT NULL,
                last_active_at_utc INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_rate_limits (
                ip_address TEXT PRIMARY KEY,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until_utc INTEGER NOT NULL DEFAULT 0,
                last_attempt_at_utc INTEGER NOT NULL
            );
            """
        )
        conn.commit()


def verify_ecdsa_signature(public_key_pem: str, canonical_data: str, signature_b64: str) -> bool:
    if not serialization or not public_key_pem or not signature_b64:
        return False
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        sig_bytes = base64.b64decode(signature_b64)
        public_key.verify(sig_bytes, canonical_data.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _gen_random_part(length: int = 4) -> str:
    chars = string.ascii_uppercase + string.digits
    clean_chars = [c for c in chars if c not in {"0", "O", "1", "I"}]
    return "".join(secrets.choice(clean_chars) for _ in range(length))


def generate_key_code(duration_days: int) -> str:
    tag = f"{duration_days}D" if duration_days < 365 else "1Y"
    p1 = _gen_random_part(4)
    p2 = _gen_random_part(4)
    return f"LIVE-{tag}-{p1}-{p2}"


def create_licenses(count: int, duration_days: int, note: str = "") -> list[str]:
    init_db()
    created = []
    with _get_connection() as conn:
        for _ in range(count):
            while True:
                code = generate_key_code(duration_days)
                try:
                    conn.execute(
                        "INSERT INTO licenses (key_code, duration_days, status, note) VALUES (?, ?, 'UNACTIVATED', ?)",
                        (code, duration_days, note),
                    )
                    created.append(code)
                    break
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
    return created


def register_installation(
    installation_id: str,
    device_fingerprint: str,
    public_key_pem: str | None = None,
) -> dict[str, Any]:
    init_db()
    inst_id = (installation_id or "").strip()
    fingerprint = (device_fingerprint or "").strip()
    if not fingerprint or len(fingerprint) < 8 or not inst_id:
        return {"status": "INVALID_DEVICE", "is_valid": False, "message": "Mã thiết bị / installation không hợp lệ"}

    now = _now_utc()
    with _get_connection() as conn:
        # Check if this exact installation exists
        cur = conn.execute("SELECT * FROM device_installations WHERE installation_id = ?", (inst_id,))
        inst = cur.fetchone()
        if inst:
            if public_key_pem and not inst["public_key_pem"]:
                conn.execute(
                    "UPDATE device_installations SET public_key_pem = ? WHERE installation_id = ?",
                    (public_key_pem.strip(), inst_id),
                )
                conn.commit()
            return handshake(fingerprint, inst_id)

        # Check if hardware fingerprint already had a trial
        cur = conn.execute("SELECT * FROM device_trials WHERE device_fingerprint = ?", (fingerprint,))
        trial = cur.fetchone()
        if trial:
            first_seen = trial["first_seen_at"]
            trial_expires = trial["expires_at"]
        else:
            first_seen = now
            trial_expires = now + 3 * 86400
            conn.execute(
                "INSERT INTO device_trials (device_fingerprint, first_seen_at, expires_at) VALUES (?, ?, ?)",
                (fingerprint, first_seen, trial_expires),
            )

        conn.execute(
            """
            INSERT INTO device_installations 
            (installation_id, device_fingerprint, public_key_pem, first_seen_at_utc, trial_expires_at_utc, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, 'TRIAL', ?)
            """,
            (inst_id, fingerprint, public_key_pem.strip() if public_key_pem else None, first_seen, trial_expires, now),
        )
        conn.commit()

    return handshake(fingerprint, inst_id)


def handshake(device_fingerprint: str, installation_id: str | None = None) -> dict[str, Any]:
    init_db()
    if not device_fingerprint or len(device_fingerprint.strip()) < 8:
        return {
            "status": "INVALID_DEVICE",
            "is_valid": False,
            "is_vip": False,
            "is_trial": False,
            "message": "Mã thiết bị không hợp lệ (Missing Device ID)",
        }

    device = device_fingerprint.strip()
    now = _now_utc()

    with _get_connection() as conn:
        # 1. Check active VIP license
        cur = conn.execute(
            "SELECT * FROM licenses WHERE bound_device = ? AND status = 'ACTIVE' ORDER BY expires_at DESC LIMIT 1",
            (device,),
        )
        row = cur.fetchone()
        if row:
            expires_at = row["expires_at"]
            if expires_at and now <= expires_at:
                days_left = max(1, (expires_at - now + 86399) // 86400)
                return {
                    "status": "VIP_ACTIVE",
                    "is_valid": True,
                    "is_vip": True,
                    "is_trial": False,
                    "expires_at": expires_at,
                    "days_left": days_left,
                    "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "message": f"Gói VIP đang kích hoạt (còn {days_left} ngày)",
                }
            else:
                return {
                    "status": "VIP_EXPIRED",
                    "is_valid": False,
                    "is_vip": False,
                    "is_trial": False,
                    "expires_at": expires_at,
                    "days_left": 0,
                    "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "message": "Gói VIP của bạn đã hết hạn. Vui lòng gia hạn để tiếp tục sử dụng.",
                }

        # 2. Check Trial for this device fingerprint
        cur = conn.execute("SELECT * FROM device_trials WHERE device_fingerprint = ?", (device,))
        trial = cur.fetchone()
        if not trial:
            expires_at = now + 3 * 86400
            conn.execute(
                "INSERT INTO device_trials (device_fingerprint, first_seen_at, expires_at) VALUES (?, ?, ?)",
                (device, now, expires_at),
            )
            conn.commit()
            return {
                "status": "TRIAL_ACTIVE",
                "is_valid": True,
                "is_vip": False,
                "is_trial": True,
                "expires_at": expires_at,
                "days_left": 3,
                "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "message": "Đang sử dụng bản Dùng thử 3 ngày miễn phí (còn 3 ngày)",
            }
        else:
            expires_at = trial["expires_at"]
            if now <= expires_at:
                days_left = max(1, (expires_at - now + 86399) // 86400)
                return {
                    "status": "TRIAL_ACTIVE",
                    "is_valid": True,
                    "is_vip": False,
                    "is_trial": True,
                    "expires_at": expires_at,
                    "days_left": days_left,
                    "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "message": f"Đang sử dụng bản Dùng thử miễn phí (còn {days_left} ngày)",
                }
            else:
                return {
                    "status": "TRIAL_EXPIRED",
                    "is_valid": False,
                    "is_vip": False,
                    "is_trial": False,
                    "expires_at": expires_at,
                    "days_left": 0,
                    "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "message": "Thời gian dùng thử 3 ngày đã kết thúc. Vui lòng nhập License Key để tiếp tục.",
                }


def activate_license(
    device_fingerprint: str,
    key_code: str,
    installation_id: str | None = None,
    signature: str | None = None,
    nonce: str | None = None,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    init_db()
    if not device_fingerprint or len(device_fingerprint.strip()) < 8:
        raise ValueError("Mã thiết bị không hợp lệ")

    device = device_fingerprint.strip()
    code = key_code.strip().upper()
    now = _now_utc()

    with _get_connection() as conn:
        cur = conn.execute("SELECT * FROM licenses WHERE key_code = ?", (code,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Mã License Key không tồn tại hoặc đã nhập sai!")

        status = row["status"]
        bound = row["bound_device"]

        if status == "ACTIVE" and bound and bound != device:
            raise ValueError("Mã Key này đã được kích hoạt trên một thiết bị khác!")

        if status == "REVOKED":
            raise ValueError("Mã Key này đã bị thu hồi (Revoked)!")

        # If installation provides public key and signature, verify it
        if installation_id and signature and nonce and timestamp_utc:
            inst_row = conn.execute(
                "SELECT public_key_pem FROM device_installations WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
            if inst_row and inst_row["public_key_pem"]:
                data = f"ACTIVATE:{installation_id}:{code}:{nonce}:{timestamp_utc}"
                if not verify_ecdsa_signature(inst_row["public_key_pem"], data, signature):
                    raise ValueError("Chữ ký xác thực Android KeyStore không hợp lệ!")

        duration = row["duration_days"]
        cur_expires = row["expires_at"] or 0
        base_time = max(now, cur_expires)
        new_expires_at = base_time + duration * 86400

        conn.execute(
            """
            UPDATE licenses 
            SET status = 'ACTIVE', bound_device = ?, bound_installation_id = ?, activated_at = ?, expires_at = ? 
            WHERE key_code = ?
            """,
            (device, installation_id, now, new_expires_at, code),
        )
        if installation_id:
            conn.execute(
                "UPDATE device_installations SET active_license_key = ?, status = 'ACTIVE' WHERE installation_id = ?",
                (code, installation_id),
            )
        conn.commit()

        days_left = (new_expires_at - now + 86399) // 86400
        return {
            "status": "VIP_ACTIVE",
            "is_valid": True,
            "is_vip": True,
            "is_trial": False,
            "expires_at": new_expires_at,
            "days_left": days_left,
            "server_time_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "message": f"Kích hoạt thành công gói VIP {duration} ngày! Hạn dùng đến ngày {datetime.fromtimestamp(new_expires_at, timezone.utc).strftime('%d/%m/%Y')}.",
        }


def start_recording_lease(
    installation_id: str,
    device_fingerprint: str,
    session_id: str,
    platform: str,
    canonical_url: str,
    signature: str | None = None,
    nonce: str | None = None,
    timestamp_utc: str | None = None,
) -> tuple[bool, str, str, dict[str, Any] | None]:
    init_db()
    fingerprint = (device_fingerprint or "").strip()
    inst_id = (installation_id or "").strip()
    sess_id = (session_id or "").strip()
    if not fingerprint or len(fingerprint) < 8 or not sess_id:
        return False, "Thiết bị hoặc phiên ghi không hợp lệ", "INVALID_DEVICE", None

    now = _now_utc()
    # Check general entitlement first
    entitlement = handshake(fingerprint, inst_id)
    if not entitlement.get("is_valid"):
        return False, entitlement.get("message", "Bản quyền hoặc thời gian dùng thử đã hết hạn"), entitlement.get("status", "EXPIRED"), None

    with _get_connection() as conn:
        # Check signature if provided & public key exists
        if inst_id and signature and nonce and timestamp_utc:
            inst = conn.execute(
                "SELECT public_key_pem FROM device_installations WHERE installation_id = ?",
                (inst_id,),
            ).fetchone()
            if inst and inst["public_key_pem"]:
                canonical_data = f"LEASE_START:{inst_id}:{sess_id}:{canonical_url}:{nonce}:{timestamp_utc}"
                if not verify_ecdsa_signature(inst["public_key_pem"], canonical_data, signature):
                    return False, "Chữ ký Android KeyStore không hợp lệ", "SIGNATURE_INVALID", None

        lease_id = "lease_" + secrets.token_hex(16)
        lease_expires_at = now + settings.lease_max_duration_seconds

        conn.execute(
            """
            INSERT INTO recording_leases 
            (lease_id, installation_id, device_fingerprint, session_id, platform, canonical_url, issued_at_utc, expires_at_utc, status, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (lease_id, inst_id, fingerprint, sess_id, platform, canonical_url, now, lease_expires_at, now),
        )
        conn.commit()

    lease_info = {
        "lease_id": lease_id,
        "session_id": sess_id,
        "expires_at_utc": datetime.fromtimestamp(lease_expires_at, timezone.utc).isoformat(),
        "status": "ACTIVE",
    }
    return True, "Lease granted successfully", "ACTIVE", lease_info


def validate_lease_for_operation(
    lease_id: str,
    device_fingerprint: str | None = None,
    installation_id: str | None = None,
    session_id: str | None = None,
    canonical_url: str | None = None,
) -> tuple[bool, str]:
    if not lease_id:
        return False, "Thiếu lease_id"

    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        cur = conn.execute("SELECT * FROM recording_leases WHERE lease_id = ?", (lease_id.strip(),))
        lease = cur.fetchone()
        if not lease:
            return False, "Recording Lease không tồn tại"

        if lease["status"] == "REVOKED":
            return False, "Recording Lease đã bị thu hồi"

        if lease["status"] == "CLOSED":
            return False, "Recording Lease đã kết thúc"

        if now > lease["expires_at_utc"]:
            return False, "Recording Lease đã hết hạn thời gian (TTL expired)"

        if session_id and lease["session_id"] != session_id.strip():
            return False, "Recording Lease không khớp với phiên ghi (Session ID mismatch)"

        if device_fingerprint and lease["device_fingerprint"] != device_fingerprint.strip():
            return False, "Recording Lease không khớp với thiết bị (Device mismatch)"

        if canonical_url and lease["canonical_url"] != canonical_url.strip():
            # Allow matching canonical URL
            return False, "Recording Lease không áp dụng cho luồng LIVE này (Replay to different LIVE rejected)"

    return True, "Lease valid"


def end_recording_lease(lease_id: str, installation_id: str | None = None, session_id: str | None = None) -> bool:
    init_db()
    if not lease_id:
        return True
    with _get_connection() as conn:
        conn.execute(
            "UPDATE recording_leases SET status = 'CLOSED' WHERE lease_id = ?",
            (lease_id.strip(),),
        )
        conn.commit()
    return True


def check_access(
    device_fingerprint: str | None,
    installation_id: str | None = None,
    lease_id: str | None = None,
    session_id: str | None = None,
    canonical_url: str | None = None,
    mode: str = "inspect",
) -> tuple[bool, str, str]:
    if not device_fingerprint or not device_fingerprint.strip():
        return False, "Yêu cầu cung cấp thông tin thiết bị (Missing Device ID)", "INVALID_DEVICE"

    device = device_fingerprint.strip()

    # 1. If an active lease is provided (reconnect/refresh during an ongoing recording),
    # validate the lease. An active lease is permitted to finish the recording even if entitlement expired!
    if lease_id:
        valid_lease, reason = validate_lease_for_operation(
            lease_id=lease_id,
            device_fingerprint=device,
            installation_id=installation_id,
            session_id=session_id,
            canonical_url=canonical_url,
        )
        if valid_lease:
            return True, "Lease active", "ACTIVE"
        
        # If lease was not found or expired, check if device itself is entitled (Trial / VIP)
        ent = handshake(device, installation_id)
        if ent.get("is_valid"):
            return True, "Permitted via active device entitlement", ent.get("status", "ACTIVE")

        return False, f"Lease error: {reason}", "LEASE_INVALID"

    # 2. Check general entitlement (handshake)
    ent = handshake(device, installation_id)
    if ent.get("is_valid"):
        return True, ent.get("message", ""), ent.get("status", "ACTIVE")

    # 3. For expired devices:
    # If mode is "inspect", allow previewing metadata so user can see streamer is live.
    # If mode is "record", strictly DENY starting a new recording!
    if mode == "inspect":
        return True, ent.get("message", "Chế độ xem trước (Gói đã hết hạn)"), "EXPIRED_PREVIEW"

    return False, ent.get("message", "Bản quyền hoặc thời gian dùng thử đã kết thúc"), ent.get("status", "EXPIRED")


def get_statistics() -> dict[str, Any]:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        total_keys = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        unactivated_keys = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'UNACTIVATED'").fetchone()[0]
        active_vips = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE' AND expires_at >= ?", (now,)
        ).fetchone()[0]
        expired_vips = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE' AND expires_at < ?", (now,)
        ).fetchone()[0]

        total_trials = conn.execute("SELECT COUNT(*) FROM device_trials").fetchone()[0]
        active_trials = conn.execute(
            "SELECT COUNT(*) FROM device_trials WHERE expires_at >= ?", (now,)
        ).fetchone()[0]
        expired_trials = conn.execute(
            "SELECT COUNT(*) FROM device_trials WHERE expires_at < ?", (now,)
        ).fetchone()[0]

        active_leases = conn.execute(
            "SELECT COUNT(*) FROM recording_leases WHERE status = 'ACTIVE' AND expires_at_utc >= ?", (now,)
        ).fetchone()[0]

    return {
        "total_keys": total_keys,
        "unactivated_keys": unactivated_keys,
        "active_vips": active_vips,
        "expired_vips": expired_vips,
        "total_trials": total_trials,
        "active_trials": active_trials,
        "expired_trials": expired_trials,
        "active_leases": active_leases,
        "total_devices": total_trials + active_vips,
    }


def list_licenses(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM licenses ORDER BY (CASE WHEN status='UNACTIVATED' THEN 0 WHEN status='ACTIVE' AND expires_at>=? THEN 1 ELSE 2 END), activated_at DESC LIMIT ?",
            (now, limit),
        ).fetchall()
        result = []
        for r in rows:
            exp = r["expires_at"]
            days_left = 0
            if exp and now <= exp:
                days_left = (exp - now + 86399) // 86400
            result.append(
                {
                    "key_code": r["key_code"],
                    "duration_days": r["duration_days"],
                    "status": r["status"],
                    "bound_device": r["bound_device"],
                    "bound_installation_id": r["bound_installation_id"],
                    "activated_at": r["activated_at"],
                    "expires_at": exp,
                    "days_left": days_left,
                    "is_expired": bool(exp and now > exp),
                    "note": r["note"] or "",
                }
            )
        return result


def list_trials(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM device_trials ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            exp = r["expires_at"]
            days_left = max(0, (exp - now + 86399) // 86400) if now <= exp else 0
            result.append(
                {
                    "device_fingerprint": r["device_fingerprint"],
                    "first_seen_at": r["first_seen_at"],
                    "expires_at": exp,
                    "days_left": days_left,
                    "is_expired": now > exp,
                }
            )
        return result


def list_active_leases(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM recording_leases WHERE status = 'ACTIVE' AND expires_at_utc >= ? ORDER BY issued_at_utc DESC LIMIT ?",
            (now, limit),
        ).fetchall()
        result = []
        for r in rows:
            result.append(
                {
                    "lease_id": r["lease_id"],
                    "installation_id": r["installation_id"],
                    "device_fingerprint": r["device_fingerprint"],
                    "session_id": r["session_id"],
                    "platform": r["platform"],
                    "canonical_url": r["canonical_url"],
                    "issued_at_utc": r["issued_at_utc"],
                    "expires_at_utc": r["expires_at_utc"],
                    "remaining_seconds": max(0, r["expires_at_utc"] - now),
                }
            )
        return result


def delete_license(key_code: str) -> bool:
    init_db()
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM licenses WHERE key_code = ?", (key_code.strip(),))
        conn.commit()
        return cur.rowcount > 0


def revoke_lease(lease_id: str) -> bool:
    init_db()
    with _get_connection() as conn:
        cur = conn.execute(
            "UPDATE recording_leases SET status = 'REVOKED' WHERE lease_id = ?",
            (lease_id.strip(),),
        )
        conn.commit()
        return cur.rowcount > 0
