"""快手身份解析器单测（backend/adapters/kuaishou_identity.py）。

页面样本取自 2026-08 线上真实响应的结构（含 ``undefined`` 这类 JS 字面量），
用来锁死解析行为，避免以后重构时又退回到「随机抓到推荐流」那类事故。
"""

import pytest

from backend.adapters.identity import (
    HttpResponse,
    IdentityCache,
    IdentityKind,
    IdentityQuery,
    IdentitySource,
    PrincipalIdentity,
    ResolveContext,
    VerifyOutcome,
)
from backend.adapters.kuaishou import _extract_initial_state
from backend.adapters.kuaishou_identity import (
    InputShapeStrategy,
    KuaishouIdentityResolver,
    LiveProfileStrategy,
    PostPageStrategy,
    ShareRedirectStrategy,
    UrlPathStrategy,
    _extract_author,
    author_from_live_html,
    looks_like_principal_id,
)

FEIAFEI = "3xrgxqkqp829xz6"   # 肥阿肥（用户名 Sandy88888），任务三 100% 铁证账号
NBA = "3x6i7sguptvuyn6"       # NBA 官方号，用于「解错人」负例


def _live_html(author_json: str) -> str:
    """构造直播页 SSR 样本。故意保留 ``"authToken":undefined`` —— 线上真有，
    旧解析器就是栽在它手上（json.loads 抛异常 → 静默判 offline）。"""
    return (
        '<html><head><title>x</title></head><body>'
        '<script>window.__INITIAL_STATE__={"user":{"name":""},'
        '"liveroom":{"playList":[{"liveStream":{"caption":"标题里有个{花括号"},'
        f'"author":{author_json},"isLiving":false}}],'
        '"authToken":undefined,"token":null}};</script></body></html>'
    )


FEIAFEI_AUTHOR = (
    '{"id":"Sandy88888","name":"肥阿肥","originUserId":2117550,"living":false}'
)
NBA_AUTHOR = '{"id":"3x6i7sguptvuyn6","name":"NBA","originUserId":2086592412,"living":true}'


class _Fetcher:
    """按 URL 前缀返回预置响应，并记录调用（不联网，专测决策分支）。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers=None, timeout=10):
        self.calls.append(url)
        for prefix, resp in self.routes.items():
            if prefix in url:
                return resp
        return HttpResponse(url=url, status=404, text="")


def _ctx(fetcher, query=None):
    return ResolveContext(fetch=fetcher, partial=PrincipalIdentity(platform="kuaishou"),
                          query=query)


# ---------------------------------------------------------------- 形态判定
@pytest.mark.parametrize("value,expected", [
    (FEIAFEI, True),
    ("3xabcdef", True),
    ("Sandy88888", False),
    ("3xabc", False),          # 太短
    ("5x3abcdefghij", False),  # profile 页里的视频 id 形态，不是 principalId
    ("", False),
    (None, False),
])
def test_principal_id形态判定(value, expected):
    assert looks_like_principal_id(value) is expected


# ---------------------------------------------------------------- 输入分类
@pytest.mark.parametrize("raw,kind", [
    (FEIAFEI, IdentityKind.PRINCIPAL_ID),
    ("Sandy88888", IdentityKind.UNIQUE_NAME),
    ("肥阿肥", IdentityKind.NICKNAME),
    (f"https://www.kuaishou.com/profile/{FEIAFEI}", IdentityKind.HOME_URL),
    (f"https://live.kuaishou.com/u/{FEIAFEI}", IdentityKind.LIVE_URL),
    ("https://v.m.chenzhongtech.com/fw/photo/3x6zh63ab9abu9g", IdentityKind.POST_URL),
    ("https://v.kuaishou.com/rqgboc", IdentityKind.SHARE_URL),
    ("", IdentityKind.UNKNOWN),
])
def test_输入分类(raw, kind):
    assert KuaishouIdentityResolver(cache=IdentityCache()).classify(raw) == kind


# ---------------------------------------------------------------- SSR 解析
def test_SSR解析容忍undefined字面量():
    """线上真实坑：快手 SSR 是 JS 对象字面量，不是严格 JSON。"""
    state = _extract_initial_state(_live_html(FEIAFEI_AUTHOR))
    assert isinstance(state, dict)
    assert state["liveroom"]["playList"][0]["author"]["name"] == "肥阿肥"


def test_SSR解析容忍字符串里的花括号():
    state = _extract_initial_state(_live_html(FEIAFEI_AUTHOR))
    assert state["liveroom"]["playList"][0]["liveStream"]["caption"] == "标题里有个{花括号"


def test_SSR解析_无marker返回None():
    assert _extract_initial_state("<html>nothing</html>") is None


def test_SSR解析_截断内容返回None():
    assert _extract_initial_state('<script>window.__INITIAL_STATE__={"a":1') is None


def test_author提取():
    author = author_from_live_html(_live_html(FEIAFEI_AUTHOR))
    assert author["id"] == "Sandy88888"
    assert author["originUserId"] == 2117550


def test_author提取_风控降级页返回空():
    """风控时快手返回 200 + 空 author，必须按内容判失败而不是看状态码。"""
    assert not author_from_live_html(_live_html("{}"))


# ---------------------------------------------------------------- 作者对象抽取
def test_从作品页SSR抽作者对象():
    html = f'...{{"id":"{FEIAFEI}","name":"肥阿肥","other":1}}...'
    assert _extract_author(html) == {"id": FEIAFEI, "name": "肥阿肥"}


def test_不把photoId误当principalId():
    """作品 id 与 principalId 同形态，只有出现在作者对象/主页链接里才算人。"""
    html = '{"photoId":"3x6zh63ab9abu9g","caption":"视频"}'
    assert _extract_author(html) is None


def test_从主页链接兜底抽principalId():
    html = f'<a href="https://www.kuaishou.com/profile/{FEIAFEI}">主页</a>'
    assert _extract_author(html)["id"] == FEIAFEI


# ---------------------------------------------------------------- 各策略
def test_策略_输入即principalId零请求():
    q = IdentityQuery(raw=FEIAFEI, platform="kuaishou", kind=IdentityKind.PRINCIPAL_ID)
    f = _Fetcher({})
    got = InputShapeStrategy().run(q, _ctx(f))
    assert got.principal_id == FEIAFEI and f.calls == []


def test_策略_主页链接路径():
    q = IdentityQuery(raw=f"https://www.kuaishou.com/profile/{FEIAFEI}",
                      platform="kuaishou", kind=IdentityKind.HOME_URL)
    got = UrlPathStrategy().run(q, _ctx(_Fetcher({})))
    assert got.principal_id == FEIAFEI


def test_策略_直播链接路径同时给出live_id():
    q = IdentityQuery(raw=f"https://live.kuaishou.com/u/{FEIAFEI}",
                      platform="kuaishou", kind=IdentityKind.LIVE_URL)
    got = UrlPathStrategy().run(q, _ctx(_Fetcher({})))
    assert got.principal_id == FEIAFEI and got.live_id == FEIAFEI
    assert got.identity_source == IdentitySource.LIVE_URL


def test_策略_分享短链取终链userId():
    """任务三实证的核心链路：分享链接 userId == graphql principalId。"""
    q = IdentityQuery(raw="https://v.kuaishou.com/abc", platform="kuaishou",
                      kind=IdentityKind.SHARE_URL)
    f = _Fetcher({"v.kuaishou.com": HttpResponse(
        url=f"https://www.kuaishou.com/short-video/x?userId={FEIAFEI}&area=1",
        status=200, text="")})
    got = ShareRedirectStrategy().run(q, _ctx(f))
    assert got.principal_id == FEIAFEI and got.share_user_id == FEIAFEI


def test_策略_分享短链终链无参数时回落SSR():
    q = IdentityQuery(raw="https://v.kuaishou.com/abc", platform="kuaishou",
                      kind=IdentityKind.SHARE_URL)
    f = _Fetcher({"v.kuaishou.com": HttpResponse(
        url="https://www.kuaishou.com/short-video/x", status=200,
        text=f'{{"id":"{FEIAFEI}","name":"肥阿肥"}}')})
    got = ShareRedirectStrategy().run(q, _ctx(f))
    assert got.principal_id == FEIAFEI and got.nickname == "肥阿肥"


def test_策略_作品页种子链接():
    q = IdentityQuery(raw="Sandy88888", platform="kuaishou",
                      kind=IdentityKind.UNIQUE_NAME,
                      hints={"seed_url": "https://v.m.chenzhongtech.com/fw/photo/3xabc123456"})
    f = _Fetcher({"fw/photo": HttpResponse(
        url="x", status=200, text=f'{{"id":"{FEIAFEI}","name":"肥阿肥"}}')})
    got = PostPageStrategy().run(q, _ctx(f))
    assert got.principal_id == FEIAFEI and got.nickname == "肥阿肥"


def test_策略_作品页失效种子返回None():
    """作品被删/审核中会返回无作者对象的通用页 —— 是种子烂，不是解析 bug。"""
    q = IdentityQuery(raw="x", platform="kuaishou",
                      hints={"seed_url": "https://v.m.chenzhongtech.com/fw/photo/3xdead000000"})
    f = _Fetcher({"fw/photo": HttpResponse(url="x", status=200, text="<html>通用页</html>")})
    assert PostPageStrategy().run(q, _ctx(f)) is None


def test_策略_直播页给出昵称但用户名账号解不出principalId():
    """已设快手号的账号，author.id 是快手号而非 principalId —— 只能补别名。"""
    q = IdentityQuery(raw="Sandy88888", platform="kuaishou", kind=IdentityKind.UNIQUE_NAME)
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(FEIAFEI_AUTHOR))})
    got = LiveProfileStrategy().run(q, _ctx(f))
    assert got.principal_id == ""            # 关键：不硬造，留给别的证据
    assert got.unique_name == "Sandy88888"
    assert got.nickname == "肥阿肥"
    assert got.extra["origin_user_id"] == "2117550"


def test_策略_直播页对未设快手号的账号直接给principalId():
    q = IdentityQuery(raw=NBA, platform="kuaishou", kind=IdentityKind.PRINCIPAL_ID)
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(NBA_AUTHOR))})
    got = LiveProfileStrategy().run(q, _ctx(f))
    assert got.principal_id == NBA and got.nickname == "NBA"


# ---------------------------------------------------------------- 校验
def test_校验_originUserId一致通过():
    r = KuaishouIdentityResolver(cache=IdentityCache())
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(FEIAFEI_AUTHOR))})
    ident = PrincipalIdentity(platform="kuaishou", principal_id=FEIAFEI,
                              extra={"origin_user_id": "2117550"})
    assert r.verify(ident, _ctx(f)) == VerifyOutcome.PASS
    assert ident.confidence == 1.0
    assert ident.extra["verified_by"] == "origin_user_id"


def test_校验_解错人被拦截():
    """把 NBA 的 principalId 配给肥阿肥，必须判 FAIL —— 这是防事故的最后一道闸。"""
    r = KuaishouIdentityResolver(cache=IdentityCache())
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(NBA_AUTHOR))})
    ident = PrincipalIdentity(platform="kuaishou", principal_id=NBA,
                              extra={"origin_user_id": "2117550"})  # 输入侧是肥阿肥
    assert r.verify(ident, _ctx(f)) == VerifyOutcome.FAIL


def test_校验_请求失败记UNKNOWN而非通过():
    """网络抖动不能让配错人蒙混过关。"""
    r = KuaishouIdentityResolver(cache=IdentityCache())
    f = _Fetcher({"live.kuaishou.com": HttpResponse(url="x", status=501, text="")})
    ident = PrincipalIdentity(platform="kuaishou", principal_id=NBA,
                              extra={"origin_user_id": "2117550"})
    assert r.verify(ident, _ctx(f)) == VerifyOutcome.UNKNOWN


def test_校验_关闭开关时跳过比对():
    r = KuaishouIdentityResolver(cache=IdentityCache(), verify_identity=False)
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(NBA_AUTHOR))})
    ident = PrincipalIdentity(platform="kuaishou", principal_id=NBA,
                              extra={"origin_user_id": "2117550"})
    assert r.verify(ident, _ctx(f)) == VerifyOutcome.PASS


def test_校验_顺带富化昵称与开播态():
    r = KuaishouIdentityResolver(cache=IdentityCache())
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(NBA_AUTHOR))})
    ident = PrincipalIdentity(platform="kuaishou", principal_id=NBA)
    r.verify(ident, _ctx(f))
    assert ident.nickname == "NBA" and ident.extra["living"] is True


# ---------------------------------------------------------------- 端到端
def test_端到端_配置principalId加用户名交叉校验通过():
    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(FEIAFEI_AUTHOR))})
    ident = r.resolve("Sandy88888", hints={"principal_id": FEIAFEI})
    assert ident.principal_id == FEIAFEI
    assert ident.identity_source == IdentitySource.CONFIG
    assert ident.extra["verified"] is True


def test_端到端_只给用户名时FailSoft但保留昵称():
    """已知能力边界：匿名云 IP 下用户名无法正向解出 principalId。"""
    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(FEIAFEI_AUTHOR))})
    res = r.resolve_detailed("Sandy88888")
    assert res.ok is False                      # 主键没解出来
    assert res.partial.nickname == "肥阿肥"      # 但开播提醒够用了
    assert res.partial.extra["origin_user_id"] == "2117550"


def test_端到端_全网失败不抛异常():
    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({})  # 一律 404
    assert r.resolve("Sandy88888") is None


def test_端到端_缓存命中不重复请求():
    f = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html(FEIAFEI_AUTHOR))})
    r = KuaishouIdentityResolver(cache=IdentityCache(), fetch=f)
    r.resolve("Sandy88888", hints={"principal_id": FEIAFEI})
    n = len(f.calls)
    r.resolve("Sandy88888", hints={"principal_id": FEIAFEI})
    assert len(f.calls) == n  # 第二次零请求


# ===========================================================================
# 风控判定：区分「被限流」与「查无此人」
# ===========================================================================
# 线上实测（2026-08）：快手限流不改 HTTP 状态码，而是回 200 + 一个 57KB 的完整
# 壳页面，author 为空 {}，真相写在 playList[0].errorType：
#   {"type":2,"title":"请求过快，请稍后重试","content":"浏览其他内容","url":"/"}
# 只判 author 是否为空的话，「被限流」和「这人不存在」长得一模一样 —— 前者该退避
# 重试，后者该判身份错误。混为一谈就等于悄悄放宽了判断标准。

_RATE_LIMITED_ERR = (
    '{"type":2,"title":"请求过快，请稍后重试","content":"浏览其他内容","url":"\\u002F"}'
)
_NOT_FOUND_ERR = '{"type":1,"title":"主播不存在","content":"看看其他主播","url":"\\u002F"}'


def _live_html_err(error_json: str) -> str:
    """构造「author 为空 + 带 errorType」的降级页（线上真实形态）。"""
    return (
        '<html><body><script>window.__INITIAL_STATE__={"user":{"name":""},'
        '"liveroom":{"noticeList":[],"playList":[{"liveStream":{},"author":{},'
        f'"gameInfo":{{}},"isLiving":false,"errorType":{error_json}}}],'
        '"authToken":undefined}};</script></body></html>'
    )


def test_风控_限流降级页被识别为限流而非查无此人():
    from backend.adapters.kuaishou_identity import (
        LiveProbeStatus, _probe_from_response,
    )

    resp = HttpResponse(url="x", status=200, text=_live_html_err(_RATE_LIMITED_ERR))
    p = _probe_from_response(resp)
    assert p.status == LiveProbeStatus.RATE_LIMITED
    assert p.gated is True
    assert p.error_type == 2
    assert "请求过快" in p.error_title


def test_风控_主播不存在被识别为查无此人():
    from backend.adapters.kuaishou_identity import (
        LiveProbeStatus, _probe_from_response,
    )

    p = _probe_from_response(HttpResponse(url="x", status=200,
                                          text=_live_html_err(_NOT_FOUND_ERR)))
    assert p.status == LiveProbeStatus.NOT_FOUND
    assert p.gated is False


def test_风控_限流时校验记UNKNOWN不判身份错误():
    """限流 → 这次校验没做成，绝不能因此把好账号判死。"""
    from backend.adapters.identity import VerifyOutcome

    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html_err(_RATE_LIMITED_ERR))})
    res = r.resolve_detailed("Sandy88888", hints={"principal_id": FEIAFEI})
    assert res.identity is not None                       # 身份保留
    assert res.identity.extra.get("verified") is False    # 但标记为未校验
    assert "请求过快" in res.identity.extra.get("verify_blocked_by", "")
    assert res.identity.ttl == r.unverified_ttl           # 短 TTL，下轮重验


def test_风控_查无此人时校验判FAIL丢弃身份():
    """页面明说没这个人 → 不是网络问题，是身份真错了，必须丢弃。"""
    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html_err(_NOT_FOUND_ERR))})
    res = r.resolve_detailed("Sandy88888", hints={"principal_id": FEIAFEI})
    assert res.identity is None


def test_风控_限流会退避重试而查无此人不浪费请求():
    """限流值得再试（对方可能刚好放行），查无此人再试一百次也还是没有。"""
    from backend.adapters.kuaishou_identity import probe_live_author
    from backend.adapters.identity import PrincipalIdentity, ResolveContext, IdentityQuery

    def _ctx(f):
        return ResolveContext(fetch=f, partial=PrincipalIdentity(platform="kuaishou"),
                              query=IdentityQuery(raw="x", platform="kuaishou"))

    f1 = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html_err(_RATE_LIMITED_ERR))})
    probe_live_author("Sandy88888", _ctx(f1), retries=2, backoff=0)
    assert len(f1.calls) == 3          # 1 次 + 2 次重试

    f2 = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_live_html_err(_NOT_FOUND_ERR))})
    probe_live_author("Sandy88888", _ctx(f2), retries=2, backoff=0)
    assert len(f2.calls) == 1          # 不重试


def test_风控_节流器惩罚期会推迟后续请求():
    """撞限流后必须全进程退避 —— 惩罚期内继续请求会让惩罚续期（线上实测）。"""
    import time as _t

    from backend.adapters.kuaishou_identity import _Pacer

    p = _Pacer(0.0)
    p.penalize(0.25)
    t0 = _t.time()
    p.wait()
    assert _t.time() - t0 >= 0.2


def test_风控_空author不再被误当成有效结果():
    """author:{} 是降级页的标志，不能当成「拿到了一个没有字段的人」。"""
    assert author_from_live_html(_live_html_err(_RATE_LIMITED_ERR)) is None


# ------------------------------------------------- /profile/ 空壳页（负面结论）
#: 2026-08 实测：``live.kuaishou.com/profile/<pid>`` 是**纯客户端渲染的空壳**。
#: 它对任意 pid（含伪造的 ``3xqqqqqqqqqqqqq``）都回 200 + 54091 字节，SSR 里
#: ``authorInfoById.userInfo.originUserId`` 恒为空串，页面不含 nickname/快手号/
#: originUserId 任何真值；三份响应的差异只是字体资源名的随机 hash。
#:
#: 当初是 grep 到 HTML 里有 "originUserId" 就以为它能当限流时的备用 oracle ——
#: 那是**假阳性，命中的是模板字段名而不是值**。这里把样本固化成负例，避免以后
#: 有人重复这条弯路，也顺带焊死「解析不出 playList ≠ 查无此人」这个分支。
_PROFILE_SHELL = (
    '<html><body><script>window.__INITIAL_STATE__={'
    '"authorInfoById":{"userInfo":{"originUserId":"","name":"","headUrl":""},'
    '"sensitiveInfo":{"originUserId":""}},"liveroom":{}};</script></body></html>'
)


def test_profile空壳页_不含任何账号真值():
    """守住实证结论：这个页面拿不到 author，别再指望它当 oracle。"""
    assert author_from_live_html(_PROFILE_SHELL) is None
    state = _extract_initial_state(_PROFILE_SHELL)
    assert state is not None, "样本自检：空壳页本身是可解析的，失败说明样本写坏了"
    assert state["authorInfoById"]["userInfo"]["originUserId"] == ""


def test_profile空壳页_判为说不清而非查无此人():
    """关键分支：没有 errorType 就**不能**下「查无此人」的结论。

    若把「author 为空」一律判 NOT_FOUND，verify 会返回 FAIL 把**正确的身份丢弃**，
    日志上还显示成「账号不存在」，极难排查。只有页面明说 errorType 才算数。
    """
    from backend.adapters.kuaishou_identity import LiveProbeStatus, _probe_from_response

    probe = _probe_from_response(HttpResponse(url="x", status=200, text=_PROFILE_SHELL))
    assert probe.status == LiveProbeStatus.UNAVAILABLE
    assert probe.status != LiveProbeStatus.NOT_FOUND
    assert not probe.ok and not probe.gated


def test_profile空壳页_不会FAIL掉已配置的正确身份():
    """端到端：拿不到证据时只能记「未校验」，不能反过来否定用户配对的身份。"""
    r = KuaishouIdentityResolver(cache=IdentityCache())
    r.fetch = _Fetcher({"live.kuaishou.com": HttpResponse(
        url="x", status=200, text=_PROFILE_SHELL)})
    res = r.resolve_detailed(FEIAFEI, hints={"principal_id": FEIAFEI})
    assert res.identity is not None, "空壳页只是没证据，不构成否定身份的理由"
    assert res.identity.principal_id == FEIAFEI
    # 记为未校验 + 短 TTL：下一轮自动重验，不会把没验过的身份永久固化
    assert res.identity.extra.get("verified") is False
    assert 0 < res.identity.ttl <= 600
