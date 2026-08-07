"""阶段三 T05 接线测试：main() 正确把 kuaishou 分发到独立处理路径。

- check_status.main()：platform=="kuaishou" → fetch_kuaishou（异常兜底，不中断整轮）
- check_new_posts.main()：platform=="kuaishou" → handle_kuaishou_posts（在 Playwright 块之前，无需浏览器）
"""

import json

import pytest

import check_status as cs
import check_new_posts as cnp
from backend.adapters.base import RoomModel


# ---------------- check_status：fetch_kuaishou 映射 ----------------

def test_fetch_kuaishou_maps_live(monkeypatch):
    def fake_status(self, room_id):
        return RoomModel(
            platform="kuaishou", room_id=room_id, title="标题X", live_status=True,
            online=55, cover="http://c.jpg",
        )
    monkeypatch.setattr(
        __import__("backend.adapters.kuaishou", fromlist=["KuaishouAdapter"]).KuaishouAdapter,
        "fetch_room_status", fake_status,
    )
    r = cs.fetch_kuaishou("KS1", {})
    assert r["status"] == "live"
    assert r["title"] == "标题X"
    assert r["online"] == 55
    assert r["area"] == ""
    assert r["avatar"] == ""


def test_fetch_kuaishou_offline_and_error(monkeypatch):
    def fake_offline(self, room_id):
        return RoomModel(platform="kuaishou", room_id=room_id, live_status=False)
    monkeypatch.setattr(
        __import__("backend.adapters.kuaishou", fromlist=["KuaishouAdapter"]).KuaishouAdapter,
        "fetch_room_status", fake_offline,
    )
    assert cs.fetch_kuaishou("KS1", {})["status"] == "offline"

    def boom(self, room_id):
        raise RuntimeError("网络挂了")
    monkeypatch.setattr(
        __import__("backend.adapters.kuaishou", fromlist=["KuaishouAdapter"]).KuaishouAdapter,
        "fetch_room_status", boom,
    )
    err = cs.fetch_kuaishou("KS1", {})
    assert err["status"] == "error"
    assert "网络挂了" in err["title"]


# ---------------- check_status.main() 分发 ----------------

def test_main_routes_kuaishou(tmp_path, monkeypatch):
    rooms = [{"platform": "kuaishou", "id": "KS1", "name": "快手测试"}]
    (tmp_path / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cs, "ROOMS_FILE", str(tmp_path / "rooms.json"))
    monkeypatch.setattr(cs, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cs, "TRACKING_FILE", str(tmp_path / "tracking.json"))
    monkeypatch.setattr(cs, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(cs, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setenv("BLIVE_CONFIG", "{}")

    calls = []
    monkeypatch.setattr(cs, "fetch_kuaishou",
                        lambda rid, cfg_all=None: calls.append((rid, cfg_all)) or {
                            "status": "offline", "title": "", "online": 0, "area": "", "avatar": ""})

    cs.main()

    assert calls, "main() 未调用 fetch_kuaishou"
    assert calls[0][0] == "KS1"
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    ks = [r for r in status["rooms"] if r["platform"] == "kuaishou"]
    assert ks and ks[0]["id"] == "KS1"


# ---------------- check_new_posts.main() 分发（无需浏览器） ----------------

class _FakeContext:
    def close(self):
        pass


class _FakeBrowser:
    def new_context(self, **kw):
        return _FakeContext()

    def close(self):
        pass


class _FakePW:
    def __enter__(self):
        self.chromium = type("C", (), {"launch": staticmethod(lambda **kw: _FakeBrowser())})()
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_playwright(monkeypatch):
    import playwright.sync_api as sa
    monkeypatch.setattr(sa, "sync_playwright", lambda: _FakePW())


def test_new_posts_routes_kuaishou(tmp_path, monkeypatch, fake_playwright):
    post_rooms = [{"platform": "kuaishou", "id": "KS1", "name": "快手测试"}]
    (tmp_path / "post_rooms.json").write_text(json.dumps(post_rooms), encoding="utf-8")
    (tmp_path / "tracking.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cnp, "CONFIG_FILE", str(tmp_path / "post_rooms.json"))
    monkeypatch.setattr(cnp, "TRACKING_FILE", str(tmp_path / "tracking.json"))
    monkeypatch.setattr(cnp, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setenv("BLIVE_CONFIG", "{}")
    monkeypatch.setenv("ENABLE_POST_CHECK", "true")
    monkeypatch.setenv("DOUYIN_COOKIE", "")

    calls = []
    monkeypatch.setattr(
        cnp, "handle_kuaishou_posts",
        lambda entry, tracking, cfg_all, silence_cfg, now_str: (
            calls.append(entry) or (False, False)
        ),
    )

    cnp.main()

    assert len(calls) == 1, "main() 应将快手条目分发到 handle_kuaishou_posts 恰好一次"
    assert calls[0].get("id") == "KS1"
    assert calls[0].get("platform") == "kuaishou"
