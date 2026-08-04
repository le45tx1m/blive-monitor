"""notify_dedup 单元测试：去重账本的冷却 / 永久 / 裁剪逻辑。"""
import pytest

import notify_dedup as nd


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """把账本文件指向临时文件，避免污染仓库。"""
    p = tmp_path / "notify_dedup.json"
    monkeypatch.setattr(nd, "LEDGER_FILE", str(p))
    return p


def test_unrecorded_key_allowed(ledger):
    assert nd.should_notify("live:bilibili:123") is True


def test_record_then_suppressed_within_cooldown(ledger):
    key = "live:bilibili:123"
    nd.record(key, now=1000.0)
    # 冷却期内（默认 7200s）应被抑制
    assert nd.should_notify(key, now=1000.0 + 100) is False
    assert nd.should_notify(key, now=1000.0 + nd.LIVE_COOLDOWN_SECONDS - 1) is False


def test_allowed_after_cooldown(ledger):
    key = "live:bilibili:123"
    nd.record(key, now=1000.0)
    assert nd.should_notify(key, now=1000.0 + nd.LIVE_COOLDOWN_SECONDS) is True
    assert nd.should_notify(key, now=1000.0 + nd.LIVE_COOLDOWN_SECONDS + 10) is True


def test_permanent_mode_never_resends(ledger):
    key = "post:MS4wxxx:7490000000000000000"
    nd.record(key, now=1000.0)
    # cooldown=inf：永久不重复
    assert nd.should_notify(key, cooldown=float("inf"), now=1000.0) is False
    assert nd.should_notify(key, cooldown=float("inf"), now=1000.0 + 10**9) is False


def test_count_mode_permanent(ledger):
    key = "post:MS4wxxx:count:42"
    nd.record(key, now=0.0)
    assert nd.should_notify(key, cooldown=float("inf"), now=999999.0) is False


def test_empty_key_always_allowed(ledger):
    assert nd.should_notify("") is True


def test_prune_drops_expired_live_keeps_post(ledger):
    now = 1_000_000.0
    # 一个已过期的 live key
    nd.record("live:bilibili:old", now=now - nd.LIVE_KEY_TTL_SECONDS - 10)
    # 一个未过期的 live key
    nd.record("live:bilibili:fresh", now=now - 100)
    # 一个永久保留的 post key
    nd.record("post:MS4wxxx:abc", now=now - 10)

    nd.prune(now=now)

    ledger = nd._load()
    assert "live:bilibili:old" not in ledger
    assert "live:bilibili:fresh" in ledger
    assert "post:MS4wxxx:abc" in ledger


def test_corrupt_ledger_treated_as_allowed(ledger):
    # 写入损坏的 JSON
    ledger.write_text("{not valid json", encoding="utf-8")
    # 不应抛异常，且视为未记录 → 允许推送
    assert nd.should_notify("live:x:1") is True


# ==================== sync_from_remote ====================


def test_sync_no_token_returns_zero(ledger, monkeypatch):
    """无 GITHUB_TOKEN / GH_TOKEN 时静默降级，返回 0。"""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert nd.sync_from_remote() == 0


def test_sync_network_failure_silent(ledger, monkeypatch):
    """网络失败时不抛异常，静默返回 0。"""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "racheko-lab/blive-monitor")

    class _BoomURLError(Exception):
        pass

    def _boom(*a, **kw):
        raise _BoomURLError("network down")

    # urlopen 抛异常应被捕获
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert nd.sync_from_remote() == 0


def test_sync_merges_newer_remote(ledger, monkeypatch):
    """远端条目 ts 更新时覆盖本地；本地更新时保留本地。"""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "racheko-lab/blive-monitor")

    # 本地已有一条旧记录 + 一条更新的记录
    nd.record("live:douyin_a", now=1000.0)
    nd.record("live:douyin_b", now=5000.0)

    # 远端：douyin_a 有更新的 ts（并发 run 已推送），douyin_b 有更旧的 ts
    remote = {
        "live:douyin_a": {"ts": 2000.0},
        "live:douyin_b": {"ts": 3000.0},
        "post:MS4w:new": {"ts": 9999.0},  # 本地没有的新条目
    }

    import base64
    import json

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_urlopen(req, timeout=None):
        payload = {
            "content": base64.b64encode(
                json.dumps(remote).encode()
            ).decode()
        }
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    merged = nd.sync_from_remote()
    # douyin_a(2000>1000) + post:MS4w:new(本地无) = 2 条更新；douyin_b(3000<5000) 不覆盖
    assert merged == 2

    data = nd._load()
    assert data["live:douyin_a"]["ts"] == 2000.0  # 被远端覆盖
    assert data["live:douyin_b"]["ts"] == 5000.0  # 本地保留
    assert data["post:MS4w:new"]["ts"] == 9999.0  # 远端新增


def test_sync_no_change_when_local_newer(ledger, monkeypatch):
    """所有远端条目都不比本地新时，merged=0 且不写盘。"""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "racheko-lab/blive-monitor")

    nd.record("live:douyin_a", now=9000.0)

    remote = {"live:douyin_a": {"ts": 1000.0}}  # 远端更旧

    import base64
    import json

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(
            {"content": base64.b64encode(json.dumps(remote).encode()).decode()}
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    assert nd.sync_from_remote() == 0
    assert nd._load()["live:douyin_a"]["ts"] == 9000.0
