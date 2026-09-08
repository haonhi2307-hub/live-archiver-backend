from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .license import (
    _get_connection,
    _now_utc,
    create_licenses,
    delete_license,
    get_statistics,
    init_db,
    list_active_leases,
    list_licenses,
    list_trials,
    revoke_lease,
)
from .settings import settings

router = APIRouter()

SESSION_TTL_SECONDS = 86400  # 24 hours
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_LOCKOUT_SECONDS = 900  # 15 minutes


def _get_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else "127.0.0.1"
    trusted_list = [p.strip() for p in (settings.trusted_proxies or "").split(",") if p.strip()]
    if direct_ip in trusted_list or "127.0.0.1" in trusted_list:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            try:
                ipaddress.ip_address(client_ip)
                return client_ip
            except ValueError:
                pass
    return direct_ip


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        row = conn.execute("SELECT * FROM admin_rate_limits WHERE ip_address = ?", (ip,)).fetchone()
        if row:
            if row["locked_until_utc"] > now:
                return False, row["locked_until_utc"] - now
            if now - row["last_attempt_at_utc"] > 1800:
                conn.execute(
                    "UPDATE admin_rate_limits SET failed_attempts = 0, locked_until_utc = 0 WHERE ip_address = ?",
                    (ip,),
                )
                conn.commit()
    return True, 0


def _record_login_attempt(ip: str, success: bool) -> None:
    init_db()
    now = _now_utc()
    with _get_connection() as conn:
        row = conn.execute("SELECT * FROM admin_rate_limits WHERE ip_address = ?", (ip,)).fetchone()
        if success:
            if row:
                conn.execute(
                    "UPDATE admin_rate_limits SET failed_attempts = 0, locked_until_utc = 0, last_attempt_at_utc = ? WHERE ip_address = ?",
                    (now, ip),
                )
            else:
                conn.execute(
                    "INSERT INTO admin_rate_limits (ip_address, failed_attempts, locked_until_utc, last_attempt_at_utc) VALUES (?, 0, 0, ?)",
                    (ip, now),
                )
        else:
            attempts = (row["failed_attempts"] if row else 0) + 1
            locked_until = now + RATE_LIMIT_LOCKOUT_SECONDS if attempts >= RATE_LIMIT_MAX_ATTEMPTS else 0
            if row:
                conn.execute(
                    "UPDATE admin_rate_limits SET failed_attempts = ?, locked_until_utc = ?, last_attempt_at_utc = ? WHERE ip_address = ?",
                    (attempts, locked_until, now, ip),
                )
            else:
                conn.execute(
                    "INSERT INTO admin_rate_limits (ip_address, failed_attempts, locked_until_utc, last_attempt_at_utc) VALUES (?, ?, ?, ?)",
                    (ip, attempts, locked_until, now),
                )
        conn.commit()


def create_admin_session() -> tuple[str, str]:
    now = _now_utc()
    secret = (settings.admin_secret or settings.api_key or "admin888").strip()
    raw_body = f"la_{now}_" + secrets.token_hex(8)
    sig = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()[:16]
    stateless_token = f"{raw_body}_{sig}"
    csrf_token = secrets.token_urlsafe(24)

    try:
        init_db()
        token_hash = hashlib.sha256(stateless_token.encode("utf-8")).hexdigest()
        session_id = "sess_" + secrets.token_hex(16)
        expires_at = now + SESSION_TTL_SECONDS
        with _get_connection() as conn:
            conn.execute(
                """
                INSERT INTO admin_sessions (session_id, token_hash, csrf_token, created_at_utc, expires_at_utc, last_active_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, token_hash, csrf_token, now, expires_at, now),
            )
            conn.commit()
    except Exception:
        pass

    return stateless_token, csrf_token


def verify_admin_session(request: Request) -> tuple[bool, dict[str, Any] | None]:
    raw_token = request.cookies.get("la_admin_session") or request.cookies.get("la_admin_auth")
    if not raw_token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            raw_token = auth_hdr[7:].strip()

    if not raw_token:
        return False, None

    secret = (settings.admin_secret or settings.api_key or "admin888").strip()

    # 1. Direct password match (never breaks even if DB wipes)
    if hmac.compare_digest(raw_token, secret):
        return True, {"csrf_token": "master_csrf"}

    # 2. Stateless HMAC token validation
    parts = raw_token.split("_")
    if len(parts) >= 4 and parts[0] == "la":
        try:
            ts = int(parts[1])
            sig = parts[-1]
            raw_body = "_".join(parts[:-1])
            expected_sig = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()[:16]
            if hmac.compare_digest(sig, expected_sig):
                now = _now_utc()
                if now - ts < SESSION_TTL_SECONDS:
                    return True, {"csrf_token": "stateless_csrf"}
        except Exception:
            pass

    # 3. Database session lookup
    try:
        init_db()
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = _now_utc()
        with _get_connection() as conn:
            row = conn.execute("SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,)).fetchone()
            if row and now <= row["expires_at_utc"]:
                return True, dict(row)
    except Exception:
        pass

    return False, None


def destroy_admin_session(request: Request) -> None:
    raw_token = request.cookies.get("la_admin_session")
    if not raw_token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            raw_token = auth_hdr[7:].strip()
    if raw_token:
        try:
            init_db()
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            with _get_connection() as conn:
                conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
                conn.commit()
        except Exception:
            pass


def verify_csrf(request: Request, session: dict[str, Any], payload: dict | None = None) -> bool:
    expected = session.get("csrf_token")
    if not expected or expected in ("master_csrf", "stateless_csrf"):
        return True
    received = request.headers.get("X-CSRF-Token")
    if not received and payload and isinstance(payload, dict):
        received = payload.get("csrf_token")
    if not received:
        return False
    return hmac.compare_digest(received, expected)


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_page(request: Request):
    is_auth, session = verify_admin_session(request)
    csrf_token = session.get("csrf_token") if session else ""

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Live Archiver Pro • Admin Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body {{ background-color: #0f172a; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }}
    .card-glass {{ background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }}
  </style>
</head>
<body class="min-h-screen p-4 md:p-8">
  <div class="max-w-6xl mx-auto space-y-6">

    <!-- Header -->
    <header class="flex flex-col md:flex-row md:items-center justify-between gap-4 card-glass p-5 rounded-2xl">
      <div class="flex items-center space-x-3">
        <div class="w-12 h-12 rounded-xl bg-gradient-to-tr from-purple-600 to-indigo-500 flex items-center justify-center text-white text-2xl shadow-lg shadow-purple-500/30">
          <i class="fa-solid fa-video"></i>
        </div>
        <div>
          <h1 class="text-xl md:text-2xl font-bold bg-gradient-to-r from-purple-400 to-indigo-300 bg-clip-text text-transparent">Live Archiver Pro</h1>
          <p class="text-xs text-slate-400">Hệ Thống Quản Lý Bản Quyền & Lease Phiên Ghi</p>
        </div>
      </div>
      <div class="flex items-center space-x-3">
        <button onclick="refreshData()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-xl text-sm font-medium transition flex items-center gap-2 border border-slate-700">
          <i class="fa-solid fa-rotate"></i> Làm mới
        </button>
        <button onclick="logout()" class="px-4 py-2 bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/30 rounded-xl text-sm font-medium transition flex items-center gap-2">
          <i class="fa-solid fa-right-from-bracket"></i> Đăng xuất
        </button>
      </div>
    </header>

    <!-- Auth Guard Modal / Login Overlay -->
    <div id="login-modal" class="{'hidden' if is_auth else 'fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4'}">
      <div class="card-glass p-6 md:p-8 rounded-2xl max-w-md w-full border border-purple-500/30 shadow-2xl">
        <div class="text-center mb-6">
          <div class="w-16 h-16 rounded-2xl bg-purple-500/20 text-purple-400 mx-auto flex items-center justify-center text-3xl mb-3 border border-purple-500/30">
            <i class="fa-solid fa-lock"></i>
          </div>
          <h2 class="text-xl font-bold">Xác Thực Quản Trị</h2>
          <p class="text-xs text-slate-400 mt-1">Vui lòng nhập mật khẩu ADMIN_SECRET của Server</p>
        </div>
        <div class="space-y-4">
          <input type="password" id="admin-pass" placeholder="Nhập ADMIN_SECRET..." class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-purple-500 text-white">
          <div id="login-err" class="text-xs text-rose-400 hidden"></div>
          <button onclick="submitLogin()" class="w-full py-3 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 rounded-xl font-semibold text-sm shadow-lg shadow-purple-500/30 transition">
            Đăng Nhập Quản Trị
          </button>
        </div>
      </div>
    </div>

    <!-- Stats Overview -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="card-glass p-5 rounded-2xl border-l-4 border-l-purple-500">
        <div class="flex justify-between items-start">
          <span class="text-xs text-slate-400 font-medium">👑 Gói VIP Đang Dùng</span>
          <i class="fa-solid fa-crown text-purple-400"></i>
        </div>
        <div id="stat-active-vips" class="text-2xl font-bold mt-2">--</div>
        <div class="text-[11px] text-slate-400 mt-1">Đang trả phí</div>
      </div>
      <div class="card-glass p-5 rounded-2xl border-l-4 border-l-amber-500">
        <div class="flex justify-between items-start">
          <span class="text-xs text-slate-400 font-medium">⏳ Đang Dùng Thử</span>
          <i class="fa-solid fa-clock text-amber-400"></i>
        </div>
        <div id="stat-active-trials" class="text-2xl font-bold mt-2">--</div>
        <div class="text-[11px] text-slate-400 mt-1">Bản 3 ngày miễn phí</div>
      </div>
      <div class="card-glass p-5 rounded-2xl border-l-4 border-l-emerald-500">
        <div class="flex justify-between items-start">
          <span class="text-xs text-slate-400 font-medium">🔑 Key Chưa Dùng</span>
          <i class="fa-solid fa-key text-emerald-400"></i>
        </div>
        <div id="stat-unact-keys" class="text-2xl font-bold mt-2">--</div>
        <div class="text-[11px] text-slate-400 mt-1">Sẵn sàng bán</div>
      </div>
      <div class="card-glass p-5 rounded-2xl border-l-4 border-l-blue-500">
        <div class="flex justify-between items-start">
          <span class="text-xs text-slate-400 font-medium">⚡ Recording Leases</span>
          <i class="fa-solid fa-circle-play text-blue-400"></i>
        </div>
        <div id="stat-active-leases" class="text-2xl font-bold mt-2">--</div>
        <div class="text-[11px] text-slate-400 mt-1">Phiên đang ghi trực tiếp</div>
      </div>
    </div>

    <!-- Generator Section -->
    <div class="card-glass p-6 rounded-2xl">
      <div class="flex items-center space-x-2 mb-4">
        <i class="fa-solid fa-wand-magic-sparkles text-purple-400 text-lg"></i>
        <h2 class="text-lg font-bold">Tạo License Key Bán Hàng</h2>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-4 items-end">
        <div>
          <label class="block text-xs text-slate-400 mb-1.5 font-medium">Gói thời hạn</label>
          <select id="key-days" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-purple-500 text-white">
            <option value="30">30 Ngày (1 Tháng)</option>
            <option value="90">90 Ngày (3 Tháng)</option>
            <option value="365">365 Ngày (1 Năm VIP)</option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-slate-400 mb-1.5 font-medium">Số lượng key</label>
          <input type="number" id="key-count" value="1" min="1" max="100" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-purple-500 text-white">
        </div>
        <div>
          <label class="block text-xs text-slate-400 mb-1.5 font-medium">Ghi chú (Tên khách/Zalo)</label>
          <input type="text" id="key-note" placeholder="Ví dụ: Khach_Zalo_0988..." class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-purple-500 text-white">
        </div>
        <div>
          <button onclick="createKeys()" class="w-full py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 rounded-xl font-semibold text-sm shadow-lg shadow-purple-500/30 transition flex items-center justify-center gap-2">
            <i class="fa-solid fa-plus"></i> Tạo Mã Key
          </button>
        </div>
      </div>

      <!-- Generated Result Box -->
      <div id="generated-box" class="mt-4 p-4 bg-purple-950/40 border border-purple-500/30 rounded-xl hidden">
        <div class="flex justify-between items-center mb-2">
          <span class="text-xs font-semibold text-purple-300">Mã Key Vừa Tạo:</span>
          <button onclick="copyGeneratedKeys()" class="text-xs text-purple-400 hover:text-purple-300 flex items-center gap-1">
            <i class="fa-regular fa-copy"></i> Sao chép tất cả
          </button>
        </div>
        <div id="generated-keys-list" class="font-mono text-sm space-y-1 text-purple-200"></div>
      </div>
    </div>

    <!-- Active Recording Leases Table -->
    <div class="card-glass p-6 rounded-2xl">
      <div class="flex justify-between items-center mb-4">
        <div class="flex items-center space-x-2">
          <i class="fa-solid fa-broadcast-tower text-blue-400"></i>
          <h2 class="text-lg font-bold">Phiên Đang Ghi Hoạt Động (Active Leases)</h2>
        </div>
        <span id="leases-count-badge" class="px-2.5 py-1 bg-blue-500/20 text-blue-300 border border-blue-500/30 rounded-full text-xs font-mono">0 lease</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="text-xs text-slate-400 bg-slate-800/60 uppercase border-b border-slate-700/60">
            <tr>
              <th class="p-3">Mã Lease</th>
              <th class="p-3">Platform</th>
              <th class="p-3">URL Đang Ghi</th>
              <th class="p-3">Thời Gian Còn Lại</th>
              <th class="p-3 text-right">Thao Tác</th>
            </tr>
          </thead>
          <tbody id="leases-tbody" class="divide-y divide-slate-800">
            <tr><td colspan="5" class="p-4 text-center text-slate-500">Đang tải dữ liệu...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Keys Table -->
    <div class="card-glass p-6 rounded-2xl">
      <div class="flex justify-between items-center mb-4">
        <div class="flex items-center space-x-2">
          <i class="fa-solid fa-key text-purple-400"></i>
          <h2 class="text-lg font-bold">Danh Sách License Key Đã Tạo</h2>
        </div>
        <span id="keys-count-badge" class="px-2.5 py-1 bg-purple-500/20 text-purple-300 border border-purple-500/30 rounded-full text-xs font-mono">0 key</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="text-xs text-slate-400 bg-slate-800/60 uppercase border-b border-slate-700/60">
            <tr>
              <th class="p-3">Mã Key</th>
              <th class="p-3">Gói</th>
              <th class="p-3">Trạng Thái</th>
              <th class="p-3">Thiết Bị Kích Hoạt</th>
              <th class="p-3">Hạn Dùng (UTC)</th>
              <th class="p-3">Ghi Chú</th>
              <th class="p-3 text-right">Thao Tác</th>
            </tr>
          </thead>
          <tbody id="keys-tbody" class="divide-y divide-slate-800">
            <tr><td colspan="7" class="p-4 text-center text-slate-500">Đang tải dữ liệu...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Trials Table -->
    <div class="card-glass p-6 rounded-2xl">
      <div class="flex justify-between items-center mb-4">
        <div class="flex items-center space-x-2">
          <i class="fa-solid fa-clock text-amber-400"></i>
          <h2 class="text-lg font-bold">Danh Sách Thiết Bị Dùng Thử 3 Ngày</h2>
        </div>
        <span id="trials-count-badge" class="px-2.5 py-1 bg-amber-500/20 text-amber-300 border border-amber-500/30 rounded-full text-xs font-mono">0 máy</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="text-xs text-slate-400 bg-slate-800/60 uppercase border-b border-slate-700/60">
            <tr>
              <th class="p-3">Mã Thiết Bị (Device Fingerprint)</th>
              <th class="p-3">Lần Đầu Mở App</th>
              <th class="p-3">Hạn Dùng Thử</th>
              <th class="p-3">Trạng Thái</th>
            </tr>
          </thead>
          <tbody id="trials-tbody" class="divide-y divide-slate-800">
            <tr><td colspan="4" class="p-4 text-center text-slate-500">Đang tải dữ liệu...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div>

  <script>
    let currentCsrfToken = '{csrf_token}';
    let generatedKeysCache = [];

    function formatDate(ts) {{
      if (!ts) return '-';
      const d = new Date(ts * 1000);
      return d.toLocaleDateString('vi-VN') + ' ' + d.toLocaleTimeString('vi-VN', {{ hour: '2-digit', minute: '2-digit' }});
    }}

    function getAuthHeaders() {{
      const token = localStorage.getItem('la_admin_token') || '';
      const headers = {{ 'Content-Type': 'application/json' }};
      if (token) headers['Authorization'] = 'Bearer ' + token;
      if (currentCsrfToken) headers['X-CSRF-Token'] = currentCsrfToken;
      return headers;
    }}

    async function submitLogin() {{
      const pass = document.getElementById('admin-pass').value.trim();
      const errEl = document.getElementById('login-err');
      errEl.classList.add('hidden');
      if (!pass) return;

      try {{
        const res = await fetch('/api/admin/login', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ password: pass }})
        }});
        const json = await res.json();
        if (res.ok) {{
          if (json.token) localStorage.setItem('la_admin_token', json.token);
          currentCsrfToken = json.csrf_token || currentCsrfToken;
          document.getElementById('login-modal').classList.add('hidden');
          refreshData();
        }} else {{
          errEl.innerText = json.detail || json.message || 'Mật khẩu không đúng!';
          errEl.classList.remove('hidden');
        }}
      }} catch (err) {{
        errEl.innerText = 'Lỗi kết nối máy chủ';
        errEl.classList.remove('hidden');
      }}
    }}

    async function logout() {{
      try {{
        await fetch('/api/admin/logout', {{
          method: 'POST',
          headers: getAuthHeaders()
        }});
      }} catch(e) {{}}
      localStorage.removeItem('la_admin_token');
      location.reload();
    }}

    async function refreshData() {{
      try {{
        const res = await fetch('/api/admin/data', {{
          headers: getAuthHeaders()
        }});
        if (res.status === 401) {{
          document.getElementById('login-modal').classList.remove('hidden');
          return;
        }}
        const data = await res.json();
        currentCsrfToken = data.csrf_token || currentCsrfToken;
        document.getElementById('login-modal').classList.add('hidden');
        renderStats(data.stats);
        renderKeys(data.licenses);
        renderTrials(data.trials);
        renderLeases(data.leases);
      }} catch (err) {{
        console.error('Error fetching admin data:', err);
      }}
    }}

    function renderStats(stats) {{
      if (!stats) return;
      document.getElementById('stat-active-vips').innerText = stats.active_vips || 0;
      document.getElementById('stat-active-trials').innerText = stats.active_trials || 0;
      document.getElementById('stat-unact-keys').innerText = stats.unactivated_keys || 0;
      document.getElementById('stat-active-leases').innerText = stats.active_leases || 0;
    }}

    function renderLeases(leases) {{
      const tbody = document.getElementById('leases-tbody');
      if (!leases || !leases.length) {{
        document.getElementById('leases-count-badge').innerText = '0 lease';
        tbody.innerHTML = '<tr><td colspan="5" class="p-4 text-center text-slate-500">Hiện không có phiên ghi nào đang chạy.</td></tr>';
        return;
      }}
      document.getElementById('leases-count-badge').innerText = leases.length + ' lease';
      tbody.innerHTML = leases.map(l => {{
        const mins = Math.floor(l.remaining_seconds / 60);
        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="p-3 font-mono font-bold text-blue-300">${{l.lease_id}}</td>
            <td class="p-3 uppercase text-xs font-semibold">${{l.platform}}</td>
            <td class="p-3 font-mono text-xs text-slate-300 max-w-xs truncate" title="${{l.canonical_url}}">${{l.canonical_url}}</td>
            <td class="p-3 text-slate-300 font-mono text-xs">${{mins}} phút (${{formatDate(l.expires_at_utc)}})</td>
            <td class="p-3 text-right">
              <button onclick="revokeLeaseAction('${{l.lease_id}}')" class="text-rose-400 hover:text-rose-300 p-1.5 rounded hover:bg-rose-500/10 text-xs">
                <i class="fa-solid fa-ban"></i> Thu hồi
              </button>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    function renderKeys(keys) {{
      document.getElementById('keys-count-badge').innerText = keys.length + ' key';
      const tbody = document.getElementById('keys-tbody');
      if (!keys || !keys.length) {{
        tbody.innerHTML = '<tr><td colspan="7" class="p-4 text-center text-slate-500">Chưa có License Key nào. Hãy tạo key ở trên.</td></tr>';
        return;
      }}
      tbody.innerHTML = keys.map(k => {{
        let statusBadge = '';
        if (k.status === 'UNACTIVATED') {{
          statusBadge = '<span class="px-2 py-0.5 bg-emerald-500/20 text-emerald-400 rounded-full border border-emerald-500/30 text-[10px]">Chưa Dùng</span>';
        }} else if (k.is_expired) {{
          statusBadge = '<span class="px-2 py-0.5 bg-rose-500/20 text-rose-400 rounded-full border border-rose-500/30 text-[10px]">Hết Hạn</span>';
        }} else {{
          statusBadge = `<span class="px-2 py-0.5 bg-purple-500/20 text-purple-300 rounded-full border border-purple-500/30 text-[10px]">VIP (${{k.days_left}}d)</span>`;
        }}

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="p-3 font-mono font-bold text-slate-200 flex items-center gap-1.5">
              <span>${{k.key_code}}</span>
              <button onclick="navigator.clipboard.writeText('${{k.key_code}}'); alert('Đã sao chép: ' + '${{k.key_code}}');" class="text-slate-500 hover:text-purple-400 text-xs">
                <i class="fa-regular fa-copy"></i>
              </button>
            </td>
            <td class="p-3 font-semibold">${{k.duration_days}} ngày</td>
            <td class="p-3">${{statusBadge}}</td>
            <td class="p-3 font-mono text-[11px] text-slate-400">${{k.bound_device || '-'}}</td>
            <td class="p-3 text-slate-400">${{formatDate(k.expires_at)}}</td>
            <td class="p-3 text-slate-400">${{k.note || '-'}}</td>
            <td class="p-3 text-right">
              <button onclick="deleteKey('${{k.key_code}}')" class="text-rose-400 hover:text-rose-300 p-1.5 rounded hover:bg-rose-500/10 transition">
                <i class="fa-solid fa-trash"></i>
              </button>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    function renderTrials(trials) {{
      document.getElementById('trials-count-badge').innerText = trials.length + ' máy';
      const tbody = document.getElementById('trials-tbody');
      if (!trials || !trials.length) {{
        tbody.innerHTML = '<tr><td colspan="4" class="p-4 text-center text-slate-500">Chưa có thiết bị nào dùng thử.</td></tr>';
        return;
      }}
      tbody.innerHTML = trials.map(t => {{
        const statusBadge = t.is_expired
          ? '<span class="px-2 py-0.5 bg-rose-500/20 text-rose-400 rounded-full text-[10px]">Hết Hạn 3 Ngày</span>'
          : `<span class="px-2 py-0.5 bg-amber-500/20 text-amber-300 rounded-full text-[10px]">Đang Dùng (${{t.days_left}} ngày)</span>`;

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="p-3 font-mono font-medium text-slate-300">${{t.device_fingerprint}}</td>
            <td class="p-3 text-slate-400">${{formatDate(t.first_seen_at)}}</td>
            <td class="p-3 text-slate-400">${{formatDate(t.expires_at)}}</td>
            <td class="p-3">${{statusBadge}}</td>
          </tr>
        `;
      }}).join('');
    }}

    async function createKeys() {{
      const days = parseInt(document.getElementById('key-days').value) || 30;
      const count = parseInt(document.getElementById('key-count').value) || 1;
      const note = document.getElementById('key-note').value.trim();

      try {{
        const res = await fetch('/api/admin/create-keys', {{
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({{ days, count, note, csrf_token: currentCsrfToken }})
        }});
        if (res.status === 401) {{
          document.getElementById('login-modal').classList.remove('hidden');
          alert('Phiên làm việc đã hết hạn hoặc Server khởi động lại. Vui lòng đăng nhập lại mật khẩu Admin.');
          return;
        }}
        const json = await res.json();
        if (res.ok) {{
          generatedKeysCache = json.keys;
          document.getElementById('generated-box').classList.remove('hidden');
          document.getElementById('generated-keys-list').innerHTML = json.keys.map(k => `<div>${{k}}</div>`).join('');
          refreshData();
        }} else {{
          alert('Lỗi: ' + (json.detail || json.message));
        }}
      }} catch (err) {{
        alert('Lỗi kết nối máy chủ');
      }}
    }}

    function copyGeneratedKeys() {{
      if (!generatedKeysCache.length) return;
      navigator.clipboard.writeText(generatedKeysCache.join('\\n'));
      alert('Đã sao chép ' + generatedKeysCache.length + ' License Key vào bộ nhớ tạm!');
    }}

    async function deleteKey(keyCode) {{
      if (!confirm('Bạn có chắc chắn muốn xóa key: ' + keyCode + ' ?')) return;
      try {{
        const res = await fetch('/api/admin/delete-key', {{
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({{ key_code: keyCode, csrf_token: currentCsrfToken }})
        }});
        if (res.ok) refreshData();
      }} catch (err) {{
        console.error(err);
      }}
    }}

    async function revokeLeaseAction(leaseId) {{
      if (!confirm('Bạn có chắc chắn muốn thu hồi Lease: ' + leaseId + ' ?')) return;
      try {{
        const res = await fetch('/api/admin/revoke-lease', {{
          method: 'POST',
          headers: getAuthHeaders(),
          body: JSON.stringify({{ lease_id: leaseId, csrf_token: currentCsrfToken }})
        }});
        if (res.ok) refreshData();
      }} catch (err) {{
        console.error(err);
      }}
    }}

    // Auto load on init
    refreshData();
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.post("/api/admin/login")
async def api_admin_login(req: dict, request: Request, response: Response):
    client_ip = _get_client_ip(request)
    allowed, wait_sec = _check_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Quá nhiều lần thử sai. IP bị tạm khóa trong {wait_sec} giây.",
        )

    expected_secret = (settings.admin_secret or "").strip()
    if not expected_secret:
        raise HTTPException(status_code=403, detail="Admin Portal chưa được cấu hình ADMIN_SECRET trên Server.")

    provided_pass = str(req.get("password", "")).strip()
    if not hmac.compare_digest(provided_pass, expected_secret):
        _record_login_attempt(client_ip, success=False)
        raise HTTPException(status_code=401, detail="Mật khẩu ADMIN_SECRET không chính xác.")

    _record_login_attempt(client_ip, success=True)
    raw_token, csrf_token = create_admin_session()

    response.set_cookie(
        key="la_admin_session",
        value=raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "token": raw_token, "csrf_token": csrf_token, "message": "Đăng nhập thành công"}


@router.post("/api/admin/logout")
async def api_admin_logout(request: Request, response: Response):
    destroy_admin_session(request)
    response.delete_cookie(key="la_admin_session", path="/")
    return {"ok": True, "message": "Đã đăng xuất"}


@router.get("/api/admin/data")
async def get_admin_data(request: Request):
    is_auth, session = verify_admin_session(request)
    if not is_auth or not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {
        "stats": get_statistics(),
        "licenses": list_licenses(200),
        "trials": list_trials(100),
        "leases": list_active_leases(50),
        "csrf_token": session.get("csrf_token"),
    }


@router.post("/api/admin/create-keys")
async def api_create_keys(req: dict, request: Request):
    is_auth, session = verify_admin_session(request)
    if not is_auth or not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not verify_csrf(request, session, req):
        raise HTTPException(status_code=403, detail="CSRF Token Invalid")

    days = int(req.get("days", 30))
    count = int(req.get("count", 1))
    note = str(req.get("note", ""))
    keys = create_licenses(count=count, duration_days=days, note=note)
    return {"ok": True, "count": len(keys), "keys": keys}


@router.post("/api/admin/delete-key")
async def api_delete_key(req: dict, request: Request):
    is_auth, session = verify_admin_session(request)
    if not is_auth or not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not verify_csrf(request, session, req):
        raise HTTPException(status_code=403, detail="CSRF Token Invalid")

    key_code = str(req.get("key_code", ""))
    success = delete_license(key_code)
    return {"ok": success}


@router.post("/api/admin/revoke-lease")
async def api_revoke_lease(req: dict, request: Request):
    is_auth, session = verify_admin_session(request)
    if not is_auth or not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not verify_csrf(request, session, req):
        raise HTTPException(status_code=403, detail="CSRF Token Invalid")

    lease_id = str(req.get("lease_id", ""))
    success = revoke_lease(lease_id)
    return {"ok": success}
