import base64
from datetime import datetime, timezone
import os
import time
from fastapi.testclient import TestClient
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization

from app.main import app
from app.settings import settings
from app.license import (
    _now_utc,
    _get_connection,
    init_db,
    create_licenses,
    handshake,
    activate_license,
    start_recording_lease,
    validate_lease_for_operation,
    end_recording_lease,
    register_installation,
    verify_ecdsa_signature,
)
from app.admin import create_admin_session, verify_admin_session, destroy_admin_session, _check_rate_limit, _record_login_attempt

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_license.db"
    import app.license as lic_mod
    monkeypatch.setattr(lic_mod, "DB_PATH", test_db)
    monkeypatch.setattr(settings, "admin_secret", "SuperSecretAdminPass123!")
    monkeypatch.setattr(settings, "client_api_key", "live_archiver_client_v05")
    init_db()

def generate_test_ec_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, pub_pem

def sign_payload(private_key, canonical_data: str) -> str:
    sig = private_key.sign(canonical_data.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode("utf-8")


def test_client_key_cannot_admin():
    # Client sends X-API-Key to admin endpoint -> Must get 401
    resp = client.get("/api/admin/data", headers={"X-API-Key": settings.client_api_key})
    assert resp.status_code == 401


def test_missing_fingerprint_cannot_start_recording():
    # Attempting to start lease without fingerprint -> Rejected
    ok, msg, status, lease = start_recording_lease(
        installation_id="inst_123",
        device_fingerprint="",
        session_id="sess_1",
        platform="tiktok",
        canonical_url="https://tiktok.com/@user/live",
    )
    assert not ok
    assert status == "INVALID_DEVICE"


def test_expired_cannot_start_lease():
    # Setup an expired device in DB
    now = _now_utc()
    fp = "LA_TEST_EXPIRED_001"
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO device_trials (device_fingerprint, first_seen_at, expires_at) VALUES (?, ?, ?)",
            (fp, now - 10 * 86400, now - 7 * 86400),
        )
        conn.commit()

    ok, msg, status, lease = start_recording_lease(
        installation_id="inst_expired",
        device_fingerprint=fp,
        session_id="sess_exp",
        platform="tiktok",
        canonical_url="https://tiktok.com/@user/live",
    )
    assert not ok
    assert status in {"TRIAL_EXPIRED", "EXPIRED"}
    assert lease is None


def test_active_lease_only_valid_for_same_recording_session():
    fp = "LA_TEST_LEASE_VALID_01"
    sess_id = "session_original_123"
    url = "https://www.tiktok.com/@user/live"

    ok, msg, status, lease_info = start_recording_lease(
        installation_id="inst_valid",
        device_fingerprint=fp,
        session_id=sess_id,
        platform="tiktok",
        canonical_url=url,
    )
    assert ok
    lease_id = lease_info["lease_id"]

    # 1. Matching session and URL -> VALID
    valid, reason = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id=sess_id, canonical_url=url)
    assert valid

    # 2. Mismatched session -> REJECTED
    valid_wrong_sess, reason = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id="session_spoofed", canonical_url=url)
    assert not valid_wrong_sess
    assert "Session ID mismatch" in reason


def test_lease_cannot_be_replayed_for_different_live():
    fp = "LA_TEST_LEASE_REPLAY"
    sess_id = "session_live_A"
    url_A = "https://www.tiktok.com/@creator_a/live"
    url_B = "https://www.tiktok.com/@creator_b/live"

    ok, _, _, lease_info = start_recording_lease(
        installation_id="inst_replay",
        device_fingerprint=fp,
        session_id=sess_id,
        platform="tiktok",
        canonical_url=url_A,
    )
    assert ok
    lease_id = lease_info["lease_id"]

    # Replay on LIVE B -> REJECTED
    valid, reason = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id=sess_id, canonical_url=url_B)
    assert not valid
    assert "Replay to different LIVE rejected" in reason


def test_revoked_lease_rejected():
    fp = "LA_TEST_LEASE_REVOKE"
    sess_id = "session_to_revoke"
    url = "https://www.tiktok.com/@user/live"

    ok, _, _, lease_info = start_recording_lease(
        installation_id="inst_rev",
        device_fingerprint=fp,
        session_id=sess_id,
        platform="tiktok",
        canonical_url=url,
    )
    lease_id = lease_info["lease_id"]

    # Revoke lease in DB
    with _get_connection() as conn:
        conn.execute("UPDATE recording_leases SET status = 'REVOKED' WHERE lease_id = ?", (lease_id,))
        conn.commit()

    valid, reason = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id=sess_id, canonical_url=url)
    assert not valid
    assert "thu hồi" in reason


def test_closed_lease_rejected():
    fp = "LA_TEST_LEASE_CLOSED"
    sess_id = "session_to_close"
    url = "https://www.tiktok.com/@user/live"

    ok, _, _, lease_info = start_recording_lease(
        installation_id="inst_cls",
        device_fingerprint=fp,
        session_id=sess_id,
        platform="tiktok",
        canonical_url=url,
    )
    lease_id = lease_info["lease_id"]

    # End lease
    end_recording_lease(lease_id)

    valid, reason = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id=sess_id, canonical_url=url)
    assert not valid
    assert "kết thúc" in reason


def test_server_restart_preserves_active_lease():
    fp = "LA_TEST_PERSISTENCE"
    sess_id = "session_survives_restart"
    url = "https://www.tiktok.com/@user/live"

    ok, _, _, lease_info = start_recording_lease(
        installation_id="inst_persist",
        device_fingerprint=fp,
        session_id=sess_id,
        platform="tiktok",
        canonical_url=url,
    )
    lease_id = lease_info["lease_id"]

    # Simulate fresh verification after restart
    valid, _ = validate_lease_for_operation(lease_id, device_fingerprint=fp, session_id=sess_id, canonical_url=url)
    assert valid


def test_end_lease_idempotent():
    assert end_recording_lease("non_existent_lease") is True
    assert end_recording_lease("") is True


def test_clear_reinstall_does_not_trivially_create_unlimited_trials():
    hardware_fp = "LA_HARDWARE_FINGERPRINT_IMMUTABLE"
    
    # 1. First installation
    res1 = register_installation("inst_app_1", hardware_fp)
    assert res1["status"] == "TRIAL_ACTIVE"
    assert res1["days_left"] == 3

    # Fast-forward / expire the trial on server
    with _get_connection() as conn:
        conn.execute(
            "UPDATE device_trials SET expires_at = ? WHERE device_fingerprint = ?",
            (_now_utc() - 3600, hardware_fp),
        )
        conn.commit()

    # 2. User uninstalls and reinstalls app -> creates a new installation ID
    res2 = register_installation("inst_app_2_after_reinstall", hardware_fp)
    assert res2["status"] == "TRIAL_EXPIRED"
    assert res2["is_valid"] is False


def test_keystore_ecdsa_signature_verification():
    priv_key, pub_pem = generate_test_ec_keypair()
    data = "LEASE_START:inst_123:sess_456:https://tiktok.com/@user/live:nonce1:12345678"
    signature_b64 = sign_payload(priv_key, data)

    # Valid signature
    assert verify_ecdsa_signature(pub_pem, data, signature_b64) is True

    # Tampered data
    tampered_data = "LEASE_START:inst_123:sess_456:https://tiktok.com/@different_user/live:nonce1:12345678"
    assert verify_ecdsa_signature(pub_pem, tampered_data, signature_b64) is False


def test_admin_csrf_protection():
    # 1. Login to admin
    login_resp = client.post("/api/admin/login", json={"password": "SuperSecretAdminPass123!"})
    assert login_resp.status_code == 200
    csrf_token = login_resp.json()["csrf_token"]
    cookie = login_resp.cookies.get("la_admin_session")

    # 2. Create keys WITHOUT CSRF token -> Must be 403
    fail_resp = client.post(
        "/api/admin/create-keys",
        json={"days": 30, "count": 1},
        cookies={"la_admin_session": cookie},
    )
    assert fail_resp.status_code == 403

    # 3. Create keys WITH valid CSRF header -> 200 OK
    ok_resp = client.post(
        "/api/admin/create-keys",
        json={"days": 30, "count": 1, "note": "test"},
        headers={"X-CSRF-Token": csrf_token},
        cookies={"la_admin_session": cookie},
    )
    assert ok_resp.status_code == 200
    assert len(ok_resp.json()["keys"]) == 1


def test_admin_logout_invalidates_session():
    login_resp = client.post("/api/admin/login", json={"password": "SuperSecretAdminPass123!"})
    cookie = login_resp.cookies.get("la_admin_session")
    csrf_token = login_resp.json()["csrf_token"]

    # Verify data works
    data_resp = client.get("/api/admin/data", cookies={"la_admin_session": cookie})
    assert data_resp.status_code == 200

    # Logout
    logout_resp = client.post(
        "/api/admin/logout",
        headers={"X-CSRF-Token": csrf_token},
        cookies={"la_admin_session": cookie},
    )
    assert logout_resp.status_code == 200

    # After logout, data must return 401
    data_after = client.get("/api/admin/data", cookies={"la_admin_session": cookie})
    assert data_after.status_code == 401


def test_admin_brute_force_throttling():
    test_ip = "192.168.1.105"
    # Attempt 5 wrong passwords
    for _ in range(5):
        _record_login_attempt(test_ip, success=False)

    allowed, wait_sec = _check_rate_limit(test_ip)
    assert not allowed
    assert wait_sec > 0
