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


# ---------------- check_status：昵称回填 ----------------

def test_fetch_kuaishou_maps_nickname(monkeypatch):
    def fake_status(self, room_id):
        return RoomModel(
            platform="kuaishou", room_id=room_id, name="肥阿肥",
            title="标题X", live_status=True, online=55, cover="http://c.jpg",
        )
    monkeypatch.setattr(
        __import__("backend.adapters.kuaishou", fromlist=["KuaishouAdapter"]).KuaishouAdapter,
        "fetch_room_status", fake_status,
    )
    r = cs.fetch_kuaishou("KS1", {})
    assert r["nickname"] == "肥阿肥"


def test_fetch_kuaishou_maps_avatar(monkeypatch):
    def fake_status(self, room_id):
        return RoomModel(
            platform="kuaishou", room_id=room_id, name="肥阿肥",
            title="标题X", live_status=True, online=55, cover="http://c.jpg",
            avatar="https://p2-pro.a.yximgs.com/uhead/xx_s.jpg",
        )
    monkeypatch.setattr(
        __import__("backend.adapters.kuaishou", fromlist=["KuaishouAdapter"]).KuaishouAdapter,
        "fetch_room_status", fake_status,
    )
    r = cs.fetch_kuaishou("KS1", {})
    assert r["avatar"] == "https://p2-pro.a.yximgs.com/uhead/xx_s.jpg"


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


def test_main_routes_kuaishou_resolves_nickname(tmp_path, monkeypatch):
    """前端验收：rooms.json 存的是账号 ID，status.json 应回填真实昵称。"""
    rooms = [{"platform": "kuaishou", "id": "Sandy88888", "name": "Sandy88888"}]
    (tmp_path / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(cs, "ROOMS_FILE", str(tmp_path / "rooms.json"))
    monkeypatch.setattr(cs, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cs, "TRACKING_FILE", str(tmp_path / "tracking.json"))
    monkeypatch.setattr(cs, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(cs, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setenv("BLIVE_CONFIG", "{}")
    monkeypatch.setattr(
        cs, "fetch_kuaishou",
        lambda rid, cfg_all=None: {
            "status": "offline", "title": "", "online": 0, "area": "",
            "avatar": "", "nickname": "肥阿肥", "time": "2026-08-09 03:00:00",
        },
    )

    cs.main()

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    ks = [r for r in status["rooms"] if r["platform"] == "kuaishou"]
    assert ks and ks[0]["id"] == "Sandy88888"
    assert ks[0]["name"] == "肥阿肥", "前端应显示真实昵称而非账号 ID"


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
    import backend.adapters._browser as bw
    monkeypatch.setattr(sa, "sync_playwright", lambda: _FakePW())
    monkeypatch.setattr(bw, "sync_playwright", lambda: _FakePW())


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
        lambda entry, tracking, cfg_all, silence_cfg, now_str, context=None,
               shared_adapter=None: (
            calls.append(entry) or (False, False)
        ),
    )

    cnp.main()

    assert len(calls) == 1, "main() 应将快手条目分发到 handle_kuaishou_posts 恰好一次"
    assert calls[0].get("id") == "KS1"
    assert calls[0].get("platform") == "kuaishou"


def test_handle_kuaishou_posts_passes_context(monkeypatch):
    """handle_kuaishou_posts 必须把浏览器 context 透传给 fetch_new_posts。

    此前重构把快手错误地放在浏览器启动前、且不传 context，导致 fetch_new_posts
    因 context is None 恒 AdapterGated（快手新作监控恒失败）。本测试锁定透传行为。
    """
    from backend.adapters.kuaishou import KuaishouAdapter
    captured = {}
    def fake_fetch(self, author_or_room, since=None, baseline=None, context=None):
        captured["context"] = context
        return []
    monkeypatch.setattr(KuaishouAdapter, "fetch_new_posts", fake_fetch)
    # identity 解析失败回退用户名，不影响 context 透传验证。
    # 注意：resolve_kuaishou_identity 是 handle_kuaishou_posts 内部局部导入的，
    # 故必须打在源模块 backend.adapters.kuaishou 上（调用时重新从模块读取属性）。
    import backend.adapters.kuaishou as ks_mod
    monkeypatch.setattr(ks_mod, "resolve_kuaishou_identity", lambda *a, **k: None)
    cnp.handle_kuaishou_posts(
        {"platform": "kuaishou", "id": "Sandy88888", "name": "Sandy88888"},
        {}, {}, {}, "2026-08-09 13:00:00", context="FAKE_CTX",
    )
    assert captured.get("context") == "FAKE_CTX", "context 未透传给 fetch_new_posts"


# ===========================================================================
# 身份信任期：把校验预算花在刀刃上（而不是每轮重验把 IP 打进惩罚期）
# ===========================================================================
# 实测数据：一次完整交叉校验要打 2 次 live.kuaishou.com（输入侧 + principalId 侧）。
# CI 每 5 分钟一轮 → 每账号每天 576 次；而实测十几次连打就会进入 11 分钟以上的
# IP 级限流惩罚期，届时连开播状态都读不到。principalId 是账号级稳定标识，
# 「首次严格校验 + 24h 信任 + 到期重验」把请求降到 1/288，准确性损失可忽略。

import pytest

from backend.adapters.identity import HttpResponse, IdentityCache
from backend.adapters.kuaishou import (
    IDENTITY_TRUST_SEC,
    apply_identity_to_tracking,
    identity_from_tracking,
    resolve_kuaishou_identity,
)
from backend.adapters.kuaishou_identity import (
    KuaishouIdentityResolver, author_from_live_html,
)

_PID = "3xrgxqkqp829xz6"
_AUTHOR = '{"id":"Sandy88888","name":"肥阿肥","originUserId":2117550,"living":false}'
_HTML = ('<script>window.__INITIAL_STATE__={"user":{"name":""},"liveroom":{"playList":'
         '[{"liveStream":{},"author":' + _AUTHOR + ',"isLiving":false}],'
         '"authToken":undefined}};</script>')


def test_样本自检_信任期用例的HTML确实可解析():
    """样本坏掉的话，下面所有「零请求」断言都会变成假阳性。"""
    assert author_from_live_html(_HTML)


class _Counter:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None, timeout=10):
        self.calls.append(url)
        return HttpResponse(url=url, status=200, text=_HTML)


def _round(entry, tracking):
    """跑一轮身份解析，每次都用空缓存模拟 CI 的全新进程。"""
    f = _Counter()
    r = KuaishouIdentityResolver(cache=IdentityCache(), fetch=f)
    ident = resolve_kuaishou_identity(entry, entry["id"], tracking=tracking, resolver=r)
    apply_identity_to_tracking(ident, tracking)
    return ident, f.calls


def test_信任期_首轮严格校验后续轮零请求():
    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    t = {}
    ident, calls = _round(entry, t)
    assert ident.principal_id == _PID
    assert t["identity_verified"] is True
    assert len(calls) == 2                    # 输入侧 + principalId 侧，双向比对

    for _ in range(4):
        ident, calls = _round(entry, t)
        assert ident.principal_id == _PID
        assert calls == []                    # 跨进程零请求


def test_信任期_不刷新起点否则永不重验():
    """复用信任期时若顺手刷新时间戳，信任期就永远不会到期 —— 等于再也不校验。"""
    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    t = {}
    _round(entry, t)
    first = t["last_identity_refresh"]
    for _ in range(3):
        _round(entry, t)
    assert t["last_identity_refresh"] == first


def test_信任期_未校验的身份不享受信任期():
    """没验过就长期固化，等于把未经证实的猜测当成事实 —— 必须每轮重试解析。"""
    t = {"principal_id": _PID, "identity_verified": False,
         "last_identity_refresh": "2026-08-08 03:00:00"}
    assert identity_from_tracking(t) is None


def test_信任期_过期后重新校验():
    from common import bjnow
    from datetime import timedelta

    old = (bjnow() - timedelta(seconds=IDENTITY_TRUST_SEC + 60)
           ).strftime("%Y-%m-%d %H:%M:%S")
    t = {"principal_id": _PID, "identity_verified": True, "last_identity_refresh": old}
    assert identity_from_tracking(t) is None          # 过期不再信任

    fresh = bjnow().strftime("%Y-%m-%d %H:%M:%S")
    t2 = dict(t, last_identity_refresh=fresh)
    assert identity_from_tracking(t2) is not None     # 期内信任


def test_信任期_config改了principal_id立即失效():
    """用户改配置必须马上生效，否则「改了没反应」根本没法排查。"""
    from common import bjnow

    t = {"principal_id": _PID, "identity_verified": True,
         "last_identity_refresh": bjnow().strftime("%Y-%m-%d %H:%M:%S")}
    entry = {"id": "Sandy88888", "platform": "kuaishou",
             "principal_id": "3xnewnewnewnew1"}      # 用户改成了别人
    ident, calls = _round(entry, t)
    assert calls, "config 变更后必须重新解析，不能继续吃信任期"
    assert ident.principal_id == "3xnewnewnewnew1"


def test_信任期_时间戳损坏时不误信():
    """tracking 被手工改坏 / 格式变更时，宁可多花一次请求也不能瞎信。"""
    for bad in (None, "", "not-a-time", "2026/08/08 03:00:00", 12345):
        t = {"principal_id": _PID, "identity_verified": True,
             "last_identity_refresh": bad}
        assert identity_from_tracking(t) is None


def test_信任期_未来时间戳不被信任():
    """时钟回拨或数据被篡改导致的「未来时间」不能当成有效信任期。"""
    from common import bjnow
    from datetime import timedelta

    future = (bjnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    t = {"principal_id": _PID, "identity_verified": True,
         "last_identity_refresh": future}
    assert identity_from_tracking(t) is None


# ------------------------------------------------ 限流死锁（重验冷却）
#: 真实降级页：限流时 author 为空 + errorType.type=2。
_HTML_GATED = ('<script>window.__INITIAL_STATE__={"liveroom":{"playList":'
               '[{"author":{},"errorType":{"type":2,"title":"请求过快，请稍后重试",'
               '"content":"","url":"/"}}]}};</script>')


def test_样本自检_限流样本确实被识别为限流():
    """样本坏掉的话，下面的死锁用例会变成假阳性。"""
    from backend.adapters.kuaishou_identity import (
        LiveProbeStatus, _probe_from_response,
    )

    probe = _probe_from_response(HttpResponse(url="x", status=200, text=_HTML_GATED))
    assert probe.status == LiveProbeStatus.RATE_LIMITED


class _GatedCounter:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None, timeout=10):
        self.calls.append(url)
        return HttpResponse(url=url, status=200, text=_HTML_GATED)


def _gated_round(entry, tracking):
    f = _GatedCounter()
    r = KuaishouIdentityResolver(cache=IdentityCache(), fetch=f)
    ident = resolve_kuaishou_identity(entry, entry["id"], tracking=tracking, resolver=r)
    apply_identity_to_tracking(ident, tracking)
    return ident, f.calls


def test_死锁_限流期间不会每轮重复打请求():
    """回归本次实测到的死锁。

    限流 → verify 只能记 UNKNOWN → identity_verified=False → 不享受信任期
    → 下轮重新解析又打请求（限流下还会退避重试，实测每轮 6 次）→ 惩罚续期
    → 永远出不来。实测换算 1728 次/账号/天。

    修复后：首轮解析，之后在重验冷却期内复用 principal_id，零请求。
    """
    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    t = {}
    ident, first = _gated_round(entry, t)
    assert first, "首轮必须真的去解析"
    assert ident is not None and ident.principal_id == _PID

    for _ in range(4):
        ident, calls = _gated_round(entry, t)
        assert calls == [], "冷却期内不得再打 live 请求，否则惩罚会被无限续期"
        assert ident.principal_id == _PID, "复用期间 principal_id 必须保持可用"


def test_死锁_冷却期复用时如实标记为未校验():
    """打破死锁靠的是降低重试频率，**不是**把没验过的当成验过。"""
    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    t = {}
    _gated_round(entry, t)
    assert t["identity_verified"] is False

    ident, _ = _gated_round(entry, t)
    assert ident.extra.get("verified") is False, "绝不能伪装成已校验"
    assert ident.extra.get("reverify_deferred") is True
    assert t["identity_verified"] is False, "tracking 里也必须如实显示未校验"


def test_死锁_冷却期到期后会重新严格校验():
    """冷却只是推迟，不是放弃 —— 否则就成了「永远不验」。"""
    from datetime import timedelta

    from common import bjnow

    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    old = (bjnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    t = {"principal_id": _PID, "identity_verified": False,
         "last_identity_attempt": old, "last_identity_refresh": old}
    ident, calls = _gated_round(entry, t)
    assert calls, "冷却期（2h）已过，必须重新尝试校验"


def test_死锁_复用轮次不刷新尝试时间否则冷却永不到期():
    """和「不刷新信任期起点」同一个坑：复用时若也刷新，冷却期被无限推后。"""
    entry = {"id": "Sandy88888", "platform": "kuaishou", "principal_id": _PID}
    t = {}
    _gated_round(entry, t)
    stamp = t["last_identity_attempt"]

    for _ in range(3):
        _gated_round(entry, t)
        assert t["last_identity_attempt"] == stamp, \
            "复用 tracking 不算一次「尝试」，不得刷新冷却起点"


def test_死锁_未校验身份也受config变更约束():
    """用户改了 principal_id，冷却期内也必须立刻生效。"""
    from common import bjnow

    t = {"principal_id": _PID, "identity_verified": False,
         "last_identity_attempt": bjnow().strftime("%Y-%m-%d %H:%M:%S")}
    entry = {"id": "Sandy88888", "platform": "kuaishou",
             "principal_id": "3xnewnewnewnew1"}
    ident, calls = _gated_round(entry, t)
    assert calls, "config 变更必须打破冷却期"
    assert ident.principal_id == "3xnewnewnewnew1"
