from __future__ import annotations

import os
import random
import sqlite3
import string
import time
from pathlib import Path
from typing import Any

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "license.db"


def _get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_connection() as conn:
        conn.executescript(
            """
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
                activated_at INTEGER,
                expires_at INTEGER,
                note TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_licenses_bound ON licenses(bound_device);
            """
        )


def _gen_random_part(length: int = 4) -> str:
    chars = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters like 0/O, 1/I
    clean_chars = [c for c in chars if c not in {"0", "O", "1", "I"}]
    return "".join(random.choices(clean_chars, k=length))


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


def handshake(device_fingerprint: str) -> dict[str, Any]:
    init_db()
    if not device_fingerprint or len(device_fingerprint.strip()) < 8:
        return {
            "status": "INVALID_DEVICE",
            "is_valid": False,
            "message": "Mã thiết bị không hợp lệ",
        }

    device = device_fingerprint.strip()
    now = int(time.time())

    with _get_connection() as conn:
        # 1. Check if device has an active VIP license
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
                    "message": "Gói VIP của bạn đã hết hạn. Vui lòng gia hạn để tiếp tục sử dụng.",
                }

        # 2. Check 3-day Trial
        cur = conn.execute("SELECT * FROM device_trials WHERE device_fingerprint = ?", (device,))
        trial = cur.fetchone()
        if not trial:
            # First time user -> Grant 3 days free trial
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
                    "message": "Thời gian dùng thử 3 ngày đã kết thúc. Vui lòng nhập License Key để tiếp tục.",
                }


def activate_license(device_fingerprint: str, key_code: str) -> dict[str, Any]:
    init_db()
    if not device_fingerprint or len(device_fingerprint.strip()) < 8:
        raise ValueError("Mã thiết bị không hợp lệ")

    device = device_fingerprint.strip()
    code = key_code.strip().upper()
    now = int(time.time())

    with _get_connection() as conn:
        cur = conn.execute("SELECT * FROM licenses WHERE key_code = ?", (code,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Mã License Key không tồn tại hoặc đã nhập sai!")

        status = row["status"]
        bound = row["bound_device"]

        if status == "ACTIVE" and bound and bound != device:
            raise ValueError("Mã Key này đã được kích hoạt trên một thiết bị khác!")

        duration = row["duration_days"]
        # If already bound to this device, extend or reactivate
        cur_expires = row["expires_at"] or 0
        base_time = max(now, cur_expires)
        new_expires_at = base_time + duration * 86400

        conn.execute(
            """
            UPDATE licenses 
            SET status = 'ACTIVE', bound_device = ?, activated_at = ?, expires_at = ? 
            WHERE key_code = ?
            """,
            (device, now, new_expires_at, code),
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
            "message": f"Kích hoạt thành công gói VIP {duration} ngày! Hạn dùng đến ngày {time.strftime('%d/%m/%Y', time.localtime(new_expires_at))}.",
        }


def check_access(device_fingerprint: str | None) -> tuple[bool, str]:
    if not device_fingerprint:
        # If not sent, allow for backward compatibility or require
        return True, ""
    res = handshake(device_fingerprint)
    if res.get("is_valid"):
        return True, ""
    return False, res.get("message", "Bản quyền hoặc thời gian dùng thử đã hết hạn")


def get_statistics() -> dict[str, Any]:
    init_db()
    now = int(time.time())
    with _get_connection() as conn:
        total_keys = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        unactivated_keys = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'UNACTIVATED'").fetchone()[0]
        active_vips = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE' AND expires_at >= ?", (now,)).fetchone()[0]
        expired_vips = conn.execute("SELECT COUNT(*) FROM licenses WHERE status = 'ACTIVE' AND expires_at < ?", (now,)).fetchone()[0]

        total_trials = conn.execute("SELECT COUNT(*) FROM device_trials").fetchone()[0]
        active_trials = conn.execute("SELECT COUNT(*) FROM device_trials WHERE expires_at >= ?", (now,)).fetchone()[0]
        expired_trials = conn.execute("SELECT COUNT(*) FROM device_trials WHERE expires_at < ?", (now,)).fetchone()[0]

    return {
        "total_keys": total_keys,
        "unactivated_keys": unactivated_keys,
        "active_vips": active_vips,
        "expired_vips": expired_vips,
        "total_trials": total_trials,
        "active_trials": active_trials,
        "expired_trials": expired_trials,
        "total_devices": total_trials + active_vips,
    }


def list_licenses(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    now = int(time.time())
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
    now = int(time.time())
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


def delete_license(key_code: str) -> bool:
    init_db()
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM licenses WHERE key_code = ?", (key_code.strip(),))
        conn.commit()
        return cur.rowcount > 0
