"""Worker 安全契约测试。

源码断言 worker.js / cors-proxy-worker.js 的安全属性：
  - cors-proxy 白名单必须用严格匹配（hostname === / endsWith），禁用 includes（防 SSRF）
  - worker.js fetch handler 必须有 Bearer 鉴权
"""

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


# ==================== cors-proxy-worker.js ====================

def test_cors_proxy_whitelist_strict():
    """白名单必须用严格匹配，不是 includes（防 SSRF）。"""
    src = _read("cors-proxy-worker.js")
    # 禁止使用 includes 做白名单匹配
    assert "hostname.includes" not in src, (
        "cors-proxy 白名单不应使用 hostname.includes（可被子串绕过）"
    )
    # 必须用严格匹配
    assert "hostname ===" in src or "endsWith" in src, (
        "cors-proxy 白名单应使用 hostname === 或 endsWith 做严格匹配"
    )


def test_cors_proxy_handles_options():
    """必须处理 OPTIONS 预检请求。"""
    src = _read("cors-proxy-worker.js")
    assert "OPTIONS" in src, "cors-proxy 应处理 OPTIONS 预检请求"


def test_cors_proxy_covers_bilibili_and_douyin():
    """白名单必须覆盖 B站和抖音。"""
    src = _read("cors-proxy-worker.js")
    assert "api.live.bilibili.com" in src
    assert "live.douyin.com" in src


def test_cors_proxy_rejects_unknown_host():
    """非白名单主机必须返回 403。"""
    src = _read("cors-proxy-worker.js")
    assert "403" in src, "cors-proxy 应对非白名单主机返回 403"


# ==================== worker.js ====================

def test_worker_has_auth():
    """Worker fetch handler 必须有鉴权。"""
    src = _read("worker.js")
    assert "WORKER_SECRET" in src, "worker.js 应支持 WORKER_SECRET 鉴权"
    assert "Bearer" in src, "worker.js 应校验 Bearer token"


def test_worker_handles_options():
    """Worker 必须处理 OPTIONS 预检。"""
    src = _read("worker.js")
    assert "OPTIONS" in src


def test_worker_has_try_catch():
    """Worker dispatch 必须有 try/catch 错误处理。"""
    src = _read("worker.js")
    assert "catch" in src, "worker.js 应有 try/catch 错误处理"
