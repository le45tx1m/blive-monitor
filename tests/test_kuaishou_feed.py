"""快手作品流解析（backend/adapters/kuaishou_feed.py）的纯函数测试。

样本全部截自真实响应（账号 pineapple2005 / principalId 3x7ju263tgi5dn9，
以及 Sandy88888 / 3xrgxqkqp829xz6）。**CDN URL 必须用真的**：发布时间就编码在
文件名里，随手编的 URL 解不出时间，等于没测到核心逻辑。
"""

import base64
import json
import os

from backend.adapters import kuaishou_feed as kf
from backend.adapters import kuaishou_feed_core as core

# 真实 URL 样本 -------------------------------------------------------------
# 视频：/upic/<日期路径>/B<base64>_b_B<hash>.mp4
#       base64 解出 "20260807162057_180534002_204769936242_1_3"
_REAL_VIDEO_URL = (
    "https://hwmov.a.yximgs.com/upic/2026/08/07/16/"
    "BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_b_"
    "B63e35bd16b353ea0e00535842fce5dbf.mp4?clientCacheKey=3x2ywf5zitae5zg.mp4"
)
# 图集封面：/upic/<日期路径>/B<base64>_B<hash>.jpg（注意没有 _b_ 中缀）
_REAL_POSTER_URL = (
    "https://p2.a.yximgs.com/upic/2025/11/05/08/"
    "BMjAyNTExMDUwODUzNTBfMTgwNTM0MDAyXzE3OTA5MzU1NjQ1NV8xXzY=_"
    "Bbd34b6b79569510e180d2181ef37e6c0.jpg?clientCacheKey=3x65q35quat5aku.jpg"
)
# 另一个账号（Sandy88888），userId 不同 —— 用于归属校验
_REAL_SANDY_URL = (
    "https://p2.a.yximgs.com/upic/2021/12/22/21/"
    "BMjAyMTEyMjIyMTUzNDVfMjExNzU1MF82MzMxMzA5MDY2MF8xXzM=_"
    "B32b4c89222a98e61e7da56e53c6e10e5.jpg?clientCacheKey=3x6zh63ab9abu9g.jpg"
)


# ---------------- 时间/归属反解 ----------------

def test_从视频URL反解发布时间和作者():
    ts, uid = kf.decode_media_meta(_REAL_VIDEO_URL)
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2026-08-07 16:20:57"
    assert uid == "180534002"


def test_从封面URL反解发布时间和作者():
    ts, uid = kf.decode_media_meta(_REAL_POSTER_URL)
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2025-11-05 08:53:50"
    assert uid == "180534002"


def test_不同账号反解出不同userId():
    _, uid = kf.decode_media_meta(_REAL_SANDY_URL)
    assert uid == "2117550"


def test_反解时间不随系统时区漂移(monkeypatch):
    """CDN 文件名里是北京时间，换算 epoch 必须显式按 +8，不能跟随 runner 时区。

    GitHub Actions runner 默认 UTC，用裸 datetime.timestamp() 会整体偏 8 小时 ——
    这个坑本项目在 kuaishou._ts_to_bj 上已经踩过一次。
    """
    import time

    from common import epoch_to_beijing

    for tz in ("UTC", "America/New_York", "Asia/Shanghai"):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        try:
            ts, _ = kf.decode_media_meta(_REAL_VIDEO_URL)
            assert epoch_to_beijing(ts) == "2026-08-07 16:20:57", tz
        finally:
            monkeypatch.undo()
            time.tzset()


def test_无法反解时返回空():
    assert kf.decode_media_meta("https://example.com/a.jpg") == (None, "")
    assert kf.decode_media_meta("") == (None, "")
    assert kf.decode_media_meta(None) == (None, "")


def test_路径日期作为降级():
    """base64 段缺失时，退到路径里的日期（精确到小时，好过没有）。"""
    ts, uid = kf.decode_media_meta("https://p2.a.yximgs.com/upic/2026/08/07/16/plain.jpg")
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2026-08-07 16:00:00"
    assert uid == ""


# ---------------- 响应解析 ----------------

def _payload(items, result=1, living=False):
    return {"data": {"list": items, "result": result,
                     "live": {"author": {"living": living}}}}


_ITEMS = [
    {"id": "top1", "poster": _REAL_POSTER_URL, "workType": "multiple", "playUrl": "",
     "imgUrls": ["http://x/a.webp"], "author": {"id": "pineapple2005", "name": "魅力驿站"}},
    {"id": "new1", "poster": _REAL_VIDEO_URL, "workType": "video",
     "playUrl": _REAL_VIDEO_URL, "author": {"id": "pineapple2005", "name": "魅力驿站"}},
]


def test_解析真实响应():
    got = kf.parse_profile_public(_payload(_ITEMS))
    assert got["ok"] is True
    assert got["result"] == 1
    assert len(got["items"]) == 2
    assert got["author_name"] == "魅力驿站"
    assert got["author_id"] == "pineapple2005"
    assert got["living"] is False


def test_result2视为未拿到():
    """匿名被挡（预热不足），不是「没有新作品」。"""
    got = kf.parse_profile_public(_payload([], result=2))
    assert got["ok"] is False
    assert got["result"] == 2


def test_空列表即使result1也不算成功():
    assert kf.parse_profile_public(_payload([], result=1))["ok"] is False


def test_解析垃圾输入不抛异常():
    for bad in (None, "", 123, {}, {"data": "x"}, {"data": {"list": "x"}}):
        got = kf.parse_profile_public(bad)
        assert got["ok"] is False


def test_图文与视频类型判定():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    by_id = {i["photo_id"]: i for i in items}
    assert by_id["top1"]["is_image"] is True     # workType=multiple
    assert by_id["new1"]["is_image"] is False    # workType=video


def test_缺id的脏条目被丢弃():
    items = kf.parse_profile_public(_payload([{"poster": _REAL_VIDEO_URL}]))["items"]
    assert items == []


# ---------------- 排序与取最新 ----------------

def test_按时间排序而非列表顺序():
    """列表首位是 2025-11-05 的置顶，真正最新是 2026-08-07。"""
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    assert [i["photo_id"] for i in kf.sort_by_time(items)] == ["new1", "top1"]
    assert kf.pick_latest(items)["photo_id"] == "new1"


def test_无时间的条目排最后且不冒充最新():
    items = kf.parse_profile_public(_payload([
        {"id": "unknown", "poster": "https://example.com/x.jpg", "workType": "video",
         "playUrl": "", "author": {"id": "a", "name": "n"}},
    ]))["items"]
    assert kf.pick_latest(items) is None   # 宁可返回 None 也不猜


def test_空列表取最新返回None():
    assert kf.pick_latest([]) is None


# ---------------- 归属校验 ----------------

def test_归属校验通过():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    ok, why = kf.verify_ownership(items, expect_user_id="180534002",
                                  expect_author_id="pineapple2005")
    assert ok is True and why == ""


def test_归属校验拦截userId不符():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    ok, why = kf.verify_ownership(items, expect_user_id="2117550")
    assert ok is False and "userId 不匹配" in why


def test_归属校验拦截混入他人作品():
    """同一页出现两个 userId，说明抓到的不全是目标账号的作品 —— 必须整批拒绝。

    本项目在抖音上踩过「随机抓到推荐流」的坑，快手这里从数据层面就能自证。
    """
    mixed = _ITEMS + [{"id": "other", "poster": _REAL_SANDY_URL, "workType": "video",
                       "playUrl": "", "author": {"id": "Sandy88888", "name": "肥阿肥"}}]
    items = kf.parse_profile_public(_payload(mixed))["items"]
    ok, why = kf.verify_ownership(items)
    assert ok is False
    assert "userId 不唯一" in why


def test_归属校验拒绝空列表():
    assert kf.verify_ownership([])[0] is False


def test_无期望值时仅校验内部一致性():
    """首轮还没有基线，只要这批数据自洽就放行（之后自举出强校验）。"""
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    assert kf.verify_ownership(items)[0] is True


# ---------------- 文案与链接 ----------------

def test_文案剥离快手后缀():
    assert kf.clean_caption("#热辣一夏-快手") == "#热辣一夏"
    assert kf.clean_caption("某作品 - 快手") == "某作品"


def test_纯站名标题视为无文案():
    assert kf.clean_caption("快手") == ""
    assert kf.clean_caption("快手直播") == ""
    assert kf.clean_caption(None) == ""


def test_作品链接格式():
    assert kf.photo_url("3x2ywf5zitae5zg") == \
        "https://www.kuaishou.com/short-video/3x2ywf5zitae5zg"


# =================== 会话：风控 token 打废（次数/时效受限）自愈 ===================
# 风控 token 实测是「次数/时效受限」的：浏览器养熟的 token 连续命中若干次后退回
# result=2（前 ~4 次成功、之后全废）。会话必须自愈，否则后续账号会静默失败。
# 以下用 FakePage/FakeContext 模拟，不启真实浏览器。

def _resp_json(result):
    return json.dumps({
        "data": {"result": result, "list": [{
            "id": "3x2ywf5zitae5zg",
            "author": {"id": "pineapple2005", "name": "魅力驿站"},
            "poster": _REAL_VIDEO_URL, "playUrl": "", "workType": "multiple",
        }]},
    })


class _FakeResp:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def body(self):
        return self._body.encode("utf-8")


class _FakeCtx:
    """模拟浏览器 context：持有 token 配额状态（跨多次 fetch 共享）。"""

    def __init__(self, budget):
        self.budget = budget
        self.used = 0
        self.exhausted = False
        self.warm_calls = 0

    def new_page(self):
        return _FakePage(self)

    def cookies(self):
        return [{"name": n} for n in core.ANTIBOT_COOKIES]

    def add_cookies(self, cookies):
        """记录注入的 Cookie（测试用），与 Playwright context.add_cookies 同签名。"""
        self.injected = getattr(self, "injected", [])
        self.injected.extend(cookies)

    def close(self):
        pass


class _FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
        self._cb = None

    def on(self, event, cb):
        if event == "response":
            self._cb = cb

    def goto(self, url, **kw):
        if core.WARMUP_URL in url:
            self.ctx.warm_calls += 1
            # 重新预热 = 拿到新 token，配额重置（不论之前是否已耗尽）
            self.ctx.used = 0
            self.ctx.exhausted = False

    def wait_for_timeout(self, ms):
        pass

    def wait_for_response(self, pred, timeout=9000):
        c = self.ctx
        # 与 KuaishouFeedSession._warmup 的判定一致：没种下风控 token（cookies 空）
        # 或 token 打废，都返回 result=2（匿名被挡）。
        if c.exhausted or not c.cookies():
            body = _resp_json(2)
        else:
            body = _resp_json(1)
            c.used += 1
            if c.used >= c.budget:
                c.exhausted = True
        resp = _FakeResp(core.PROFILE_PUBLIC_PATH + "?x=1", body)
        if self._cb and pred(resp):
            self._cb(resp)
        return resp

    @property
    def context(self):
        return self.ctx

    def cookies(self):
        return self.ctx.cookies()

    def close(self):
        pass

    def title(self):
        return "#热辣一夏-快手"


class _FakeBrowser:
    def __init__(self, budget):
        self.budget = budget

    def new_context(self, **kw):
        return _FakeCtx(self.budget)

    def close(self):
        pass


class _FakeSrc:
    """传给 KuaishouFeedSession 的 browser_context；附带 .browser。"""

    def __init__(self, budget):
        self.browser = _FakeBrowser(budget)
        self.last_ctx = None


def _make_session(budget, max_uses=None):
    src = _FakeSrc(budget)
    real_new = src.browser.new_context

    def _new(**kw):
        src.last_ctx = real_new(**kw)
        return src.last_ctx

    src.browser.new_context = _new
    sess = kf.KuaishouFeedSession(src, user_agent="")
    if max_uses is not None:
        sess.MAX_USES_PER_TOKEN = max_uses
    return sess, src


def test_配额阈值逻辑():
    sess = kf.KuaishouFeedSession(object())
    assert sess._quota_exhausted() is False
    sess._uses = kf.KuaishouFeedSession.MAX_USES_PER_TOKEN
    assert sess._quota_exhausted() is True
    # 关闭主动重预热时永不触发
    sess.MAX_USES_PER_TOKEN = 0
    sess._uses = 999
    assert sess._quota_exhausted() is False


def test_打废_被动重预热恢复():
    """token 配额小（budget=2）：前 2 次成功、第 3 次起打废 → result=2，
    会话经强制重预热恢复，全部 fetch 成功，warm_calls >= 2。"""
    sess, src = _make_session(budget=2)
    results = [sess.fetch(f"pid{i}") for i in range(4)]
    assert all(r["ok"] for r in results), [r.get("result") for r in results]
    assert src.last_ctx.warm_calls >= 2, "打废后应至少有一次强制重预热"


def test_打废_主动重预热在配额边界():
    """主动重预热：MAX_USES_PER_TOKEN=2、token 配额充足（budget=10）时，
    每成功 2 次会话就把 _warmed 置 False，下一个账号前重新预热。
    捕获每次 fetch 返回后的 _warmed 状态（下一次 fetch 的预热会把它重新置 True，
    所以要看「返回瞬间」而非最终态）。"""
    sess, src = _make_session(budget=10, max_uses=2)
    warm_states = []
    for i in range(5):
        r = sess.fetch(f"pid{i}")
        assert r["ok"]
        warm_states.append(sess._warmed)
    # 第 2、4 次成功后应触发主动重预热 → 返回时 _warmed 已置 False
    assert warm_states[1] is False, "第 2 次成功后应主动置 _warmed=False"
    assert warm_states[3] is False, "第 4 次成功后应主动置 _warmed=False"
    assert src.last_ctx.warm_calls >= 3, "应包含初始 + 2 次主动重预热"


def test_纯IP被标记时如实记为被挡():
    """若预热始终种不下 token（cookies 为空），则每轮全 result=2，
    会话最多两轮后如实返回 ok=False，不无限空转。"""

    class _NoTokenCtx(_FakeCtx):
        def cookies(self):
            return []  # 模拟种不下风控 token

    src = _FakeSrc(10)
    real_new = src.browser.new_context

    def _new(**kw):
        src.last_ctx = _NoTokenCtx(10)
        return src.last_ctx

    src.browser.new_context = _new
    sess = kf.KuaishouFeedSession(src, user_agent="")
    r = sess.fetch("pidX")
    assert r["ok"] is False
    assert r["result"] in (2, None)
    assert "响应序列" in (r.get("detail") or "")


def test_warmup_主站打不开但已带风控token时短路():
    """CI 海外出口 www.kuaishou.com 频繁 ERR_TIMED_OUT（每次 goto 45s，3 次重试
    白烧 3+ 分钟）。若注入的登录 Cookie 已带风控 token，首次失败即应按已预热
    继续，而不是空耗满 3 次重试。"""

    class _TimeoutPage(_FakePage):
        def goto(self, url, **kw):
            if core.WARMUP_URL in url:
                self.ctx.warm_calls += 1
                raise RuntimeError("net::ERR_TIMED_OUT at " + url)
            return super().goto(url, **kw)

    class _TimeoutCtx(_FakeCtx):
        def new_page(self):
            return _TimeoutPage(self)

    ctx = _TimeoutCtx(4)
    sess = kf.KuaishouFeedSession(ctx, user_agent="")
    sess._ctx = ctx  # 直接塞入，跳过 _ensure_ctx
    page = ctx.new_page()

    assert sess._warmup(page) is True      # 带着注入 token 按已预热处理
    assert sess._warmed is True
    # 单次尝试 = 1 次 goto（旧版 networkidle+domcontentloaded 双 goto 已废弃）；短路 = 只跑 1 次尝试
    assert ctx.warm_calls == 1, "首次尝试失败即短路，不应重试满 3 次"


def test_warmup_主站打不开且无token时仍重试():
    """没有任何风控 token 时不能短路 —— 照旧重试满 MAX_WARMUP_RETRY 再放弃。"""

    class _TimeoutPage(_FakePage):
        def goto(self, url, **kw):
            if core.WARMUP_URL in url:
                self.ctx.warm_calls += 1
                raise RuntimeError("net::ERR_TIMED_OUT at " + url)
            return super().goto(url, **kw)

    class _BareCtx(_FakeCtx):
        def cookies(self):
            return []  # 无注入 cookie、也种不下 token

        def new_page(self):
            return _TimeoutPage(self)

    ctx = _BareCtx(4)
    sess = kf.KuaishouFeedSession(ctx, user_agent="")
    sess._ctx = ctx
    page = ctx.new_page()

    assert sess._warmup(page) is False
    # 无 token 不短路，照旧重试满 MAX_WARMUP_RETRY 次（每次 1 次 goto）
    assert ctx.warm_calls == sess.MAX_WARMUP_RETRY


def test_capture_visitor_cookies_keeps_did_filters_login_and_antibot():
    """首次预热捕获游客身份：保留 did/kpn/clientid，过滤登录态(userId)与受限风控
    token(kwfv1)；domain 归一化到 .kuaishou.com；并落盘供跨运行复用。"""
    kf.reset_guest_visitor_cache()

    class _Ctx:
        def cookies(self):
            return [
                {"name": "did", "value": "web_abc123", "domain": ".kuaishou.com", "path": "/"},
                {"name": "kpn", "value": "KUAISHOU_VISION", "domain": ".kuaishou.com", "path": "/"},
                {"name": "clientid", "value": "3", "domain": "www.kuaishou.com", "path": "/"},
                {"name": "kwfv1", "value": "x", "domain": ".kuaishou.com", "path": "/"},        # 受限 token，不跨运行复用
                {"name": "userId", "value": "LOGIN_SECRET", "domain": ".kuaishou.com", "path": "/"},  # 登录态，绝不缓存
            ]

    kf.KuaishouFeedSession._capture_visitor_cookies(None, _Ctx())
    names = {c["name"] for c in kf._GUEST_VISITOR_COOKIES}
    assert kf._GUEST_DID_CACHE == "web_abc123"
    assert names == {"did", "kpn", "clientid"}, names
    assert "kwfv1" not in names and "userId" not in names
    # domain 归一化到 .kuaishou.com（跨 www/live 子域可见）
    assert next(c for c in kf._GUEST_VISITOR_COOKIES if c["name"] == "clientid")["domain"] == ".kuaishou.com"
    # 已落盘
    assert os.path.exists(kf._GUEST_CACHE_FILE)
    kf.reset_guest_visitor_cache()


def test_apply_visitor_cookies_injects_cache():
    """匿名通道：_apply_visitor_cookies 把缓存的稳定 did 注入新 context（复用同一访客）。"""
    kf.reset_guest_visitor_cache()
    kf._GUEST_DID_CACHE = "web_inject9"
    kf._GUEST_VISITOR_COOKIES = [{"name": "did", "value": "web_inject9", "domain": ".kuaishou.com", "path": "/"}]

    class _Ctx:
        def __init__(self):
            self.injected = []

        def add_cookies(self, cs):
            self.injected.extend(cs)

    ctx = _Ctx()
    kf.KuaishouFeedSession._apply_visitor_cookies(None, ctx)
    assert any(c["name"] == "did" and c["value"] == "web_inject9" for c in ctx.injected)
    kf.reset_guest_visitor_cache()


def test_warmup_requires_did_not_only_antibot():
    """新逻辑：仅种下风控 token 但缺 did，不算预热成功（did 才是游客身份关键）。"""
    class _Ctx:
        def cookies(self):
            return [{"name": n} for n in core.ANTIBOT_COOKIES]  # 只有 antibot，无 did

    class _Page:
        def __init__(self, ctx):
            self.ctx = ctx

        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

        def wait_for_response(self, pred, timeout=9000):
            pass

        @property
        def context(self):
            return self.ctx

        def cookies(self):
            return self.ctx.cookies()

        def close(self):
            pass

    ctx, page = _Ctx(), _Page(_Ctx())
    sess = kf.KuaishouFeedSession(ctx, user_agent="")
    sess._warmed = False
    sess.VISITOR_WAIT_MS = 50  # 让轮询快速超时，避免等满 10s
    assert sess._warmup(page) is False


def test_warmup_plants_with_did_and_antibot():
    """did + 风控 token 都种下时，预热成功并捕获 did 进缓存。"""
    class _Ctx:
        def cookies(self):
            return [{"name": "did", "value": "web_ok1", "domain": ".kuaishou.com", "path": "/"}] + \
                   [{"name": n, "domain": ".kuaishou.com", "path": "/"} for n in core.ANTIBOT_COOKIES]

    class _Page:
        def __init__(self, ctx):
            self.ctx = ctx

        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

        def wait_for_response(self, pred, timeout=9000):
            pass

        @property
        def context(self):
            return self.ctx

        def cookies(self):
            return self.ctx.cookies()

        def close(self):
            pass

    kf.reset_guest_visitor_cache()
    ctx, page = _Ctx(), _Page(_Ctx())
    sess = kf.KuaishouFeedSession(ctx, user_agent="")
    sess._warmed = False
    sess.VISITOR_WAIT_MS = 50
    assert sess._warmup(page) is True
    assert kf._GUEST_DID_CACHE == "web_ok1"
    kf.reset_guest_visitor_cache()


def test_kuaishou_cookie_injected_to_session_context():
    """配置了 kuaishou_cookie 时，应注入到会话自建的隔离 context（domain=.kuaishou.com）。"""
    src = _FakeSrc(10)
    real_new = src.browser.new_context

    def _new(**kw):
        src.last_ctx = real_new(**kw)
        return src.last_ctx

    src.browser.new_context = _new
    sess = kf.KuaishouFeedSession(
        src, user_agent="", kuaishou_cookie="kuaishou.com=abc; passport_csrf_token=xyz")
    ctx = sess._ensure_ctx()  # 触发自建 context + 注入
    injected = getattr(ctx, "injected", [])
    assert len(injected) == 2
    assert injected[0]["domain"] == ".kuaishou.com"
    assert injected[0]["name"] == "kuaishou.com" and injected[0]["value"] == "abc"
    assert injected[1]["name"] == "passport_csrf_token"


def test_kuaishou_cookie_empty_no_inject():
    """未配置 cookie 时不应注入任何东西（保持免 Cookie 匿名通道）。"""
    src = _FakeSrc(10)
    real_new = src.browser.new_context

    def _new(**kw):
        src.last_ctx = real_new(**kw)
        return src.last_ctx

    src.browser.new_context = _new
    sess = kf.KuaishouFeedSession(src, user_agent="", kuaishou_cookie="")
    ctx = sess._ensure_ctx()
    assert getattr(ctx, "injected", []) == []


def test_parse_cookie_string_dedup_keeps_last():
    """重名 cookie（用户从多个请求合并复制时常见）应去重、保留最后一条。

    否则两条同名同域 cookie 会触发 Playwright add_cookies 的重复校验直接报错，
    导致整个 Cookie 兜底通道崩溃。实测用户抓到的 Cookie 里 kwscode / kwssectoken /
    kwpsecproductname 各出现两次且值不同。
    """
    s = "kwscode=a; kwscode=b; kwssectoken=x; kwssectoken=y; kwpsecproductname=v; kwpsecproductname=v"
    out = kf._parse_cookie_string(s, ".kuaishou.com")
    names = [c["name"] for c in out]
    assert names.count("kwscode") == 1
    assert names.count("kwssectoken") == 1
    assert names.count("kwpsecproductname") == 1
    by_name = {c["name"]: c["value"] for c in out}
    assert by_name["kwscode"] == "b"        # 保留最后一条
    assert by_name["kwssectoken"] == "y"
    assert all(c["domain"] == ".kuaishou.com" and c["path"] == "/" for c in out)
