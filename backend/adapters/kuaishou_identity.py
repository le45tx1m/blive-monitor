"""快手身份解析器（Identity Framework 的第一个平台实现）。

## 为什么需要它

快手有 **4 套并存的 id**，用户手里的和接口要的往往不是同一个：

| 标识 | 形态 | 出现位置 | graphql 能用吗 |
|---|---|---|---|
| ``unique_name``（用户名） | ``Sandy88888`` | 用户主页地址栏、名片 | ❌ 返回 ``feeds=[]`` |
| ``principal_id`` | ``3xrgxqkqp829xz6`` | 分享链接 ``userId=`` / 主页路径 / 直播路径 | ✅ **就是它** |
| ``origin_user_id`` | 纯数字 | 直播接口 ``authorIdSet`` | ❌ |
| ``photo_id`` | ``3x...``（同形态！） | 作品链接 | —— 是作品不是人 |

``principal_id`` 与 ``photo_id`` 形态完全一样，所以**不能只靠正则判断一个 3x 串是人还是作品**，
必须结合它出现的位置（路径段 / 字段名）来判定 —— 这是本模块所有正则都锚定上下文的原因。

## 已实证的证据链（2026-08 实测，≥10 账号）

1. **分享短链 302 终链**：``v.kuaishou.com/<short>`` → 终链 query 带 ``?userId=3x...``；
   已证明该 ``userId`` **等价于** graphql 需要的 principalId（~98% 置信，肥阿肥 100% 铁证）。
2. **直播间路径**：``live.kuaishou.com/u/<3x...>`` 的路径段本身就是 principalId。
3. **主页路径**：``www.kuaishou.com/profile/<3x...>``，HTTP 200 表示账号真实存在。
4. **作品页 SSR**：``v.m.chenzhongtech.com/fw/photo/<3x photoId>`` 内联 JSON 含作者对象
   ``{"id":"3x...","name":"..."}``，可同时拿到 principalId + 昵称。

## 已知不可用（别再踩）

- 用户主页是 SPA，页内视频 id 是 ``5x3...`` 形态，**不能**塞进 ``fw/photo/``（返回通用页）。
- 直播首页 ``liveCardList`` 客户端加载，SSR 为空；仅 ``limitToPlay`` 有零星在播 principalId。
- 云 IP 下所有 graphql（含搜索）大概率返 ``result=400002`` 验证码挑战 —— 因此**搜索策略只能
  当作锦上添花**，主链路必须靠上面 4 条 SSR/跳转证据走通。
- ``live.kuaishou.com/profile/<pid>`` **不能**当作 ``/u/`` 被限流时的备用 oracle。
  它是纯客户端渲染的空壳：对任意 pid（包括伪造的 ``3xqqqqqqqqqqqqq``）都回
  200 + 54091 字节，``authorInfoById.userInfo.originUserId`` 恒为空串，页面里
  **没有**昵称/快手号/originUserId 的任何真值，三份响应的差异仅是字体资源名的
  随机 hash。曾因 grep 到 HTML 含 ``"originUserId"`` 就误判它可用 —— 那是**假阳性，
  命中的是模板字段名而非值**。负例样本见 ``tests/test_kuaishou_identity.py``
  的 ``_PROFILE_SHELL``。

## 风控行为（2026-08 实测）

限流**不改 HTTP 状态码**：回 200 + 完整壳页面、``author`` 为空 ``{}``，真相写在
``playList[0].errorType`` = ``{"type":2,"title":"请求过快，请稍后重试"}``。因此
「被限流」和「查无此人」长得一模一样，只判 author 是否为空必然混淆二者（见
:class:`LiveProbeStatus` 四态）。IP 级惩罚实测**持续 >50 分钟**，且惩罚期内继续
请求会**续期** —— 所以 :class:`_Pacer` 撞限流后必须全进程退避，探测本身也要克制。

所有策略均 Fail Soft：挖不到返回 ``None``，绝不抛异常影响整轮监控。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from backend.adapters.identity import (
    http_fetch,
    IdentityKind,
    IdentityQuery,
    IdentityResolver,
    IdentitySource,
    PrincipalIdentity,
    ResolveContext,
    ResolveStrategy,
    VerifyOutcome,
)

logger = logging.getLogger(__name__)

# ---- id 形态 ----------------------------------------------------------------
#: principalId / photoId 共用的形态：3x + 至少 6 位字母数字
_ID_SHAPE = r"3x[a-zA-Z0-9]{6,}"
_RE_ID_FULL = re.compile(rf"^{_ID_SHAPE}$")

# ---- 证据正则（全部锚定上下文，避免把 photoId 误当 principalId）---------------
#: 主页链接：kuaishou.com/profile/<principalId>
_RE_PROFILE = re.compile(rf"kuaishou\.com/profile/({_ID_SHAPE})")
#: 直播链接：live.kuaishou.com/u/<principalId>
_RE_LIVE_U = re.compile(rf"live\.kuaishou\.com/u/({_ID_SHAPE})")
#: 作品链接：fw/photo/<photoId>（注意：这是作品不是人）
_RE_PHOTO = re.compile(rf"/fw/photo/({_ID_SHAPE})")
#: 分享短链
_RE_SHORT = re.compile(r"(?:v\.kuaishou\.com|v\.m\.chenzhongtech\.com/s)/([A-Za-z0-9]+)")
#: SSR 内联作者对象：{"id":"3x...","name":"昵称"} —— 作者对象里 id 紧邻 name
_RE_AUTHOR_OBJ = re.compile(
    rf'"(?:id|userId|principalId)"\s*:\s*"({_ID_SHAPE})"\s*,\s*"(?:name|userName|userText)"\s*:\s*"([^"]*)"'
)
#: 兜底：任意 "principalId":"3x..." 字段（字段名本身已足够限定语义）
_RE_PRINCIPAL_FIELD = re.compile(rf'"principalId"\s*:\s*"({_ID_SHAPE})"')

#: 尽量贴近真实浏览器：实测只带 UA 时突发请求会被回 HTTP 501（瞬时风控），
#: 补齐 Accept/Accept-Language/Upgrade-Insecure-Requests 后明显更稳。
_KS_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.kuaishou.com/",
    "Upgrade-Insecure-Requests": "1",
}
#: 判定为「瞬时」可重试的状态码：501 实测是突发限流而非真的不支持
_RETRYABLE_STATUS = (0, 429, 500, 501, 502, 503, 504)

#: 同一进程内两次快手页面请求的最小间隔（秒）。
#: 实测连续快打十几次会被判风控 —— 表现不是报错，而是**返回 200 + 空 author 的降级页**，
#: 比报错更阴险（看起来成功了，其实什么都没拿到）。节流比事后重试划算得多。
_MIN_REQUEST_INTERVAL = 0.8


class _Pacer:
    """进程内请求节流器（线程安全，纯本地不依赖外部组件）。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = float(min_interval)
        self._last = 0.0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            target = max(self._last + self.min_interval, self._blocked_until)
            if target > now:
                time.sleep(target - now)
            self._last = time.time()

    def penalize(self, seconds: float) -> None:
        """撞到限流后主动拉长下一次请求的间隔（自适应退避）。

        固定节奏撞限流是没有出路的：对方已经在惩罚期，继续按原速请求只会
        延长惩罚。这里让**全进程**的后续请求都等一等，而不只是当前这次重试。
        """
        with self._lock:
            self._blocked_until = max(self._blocked_until,
                                      time.time() + max(0.0, float(seconds)))


_pacer = _Pacer(_MIN_REQUEST_INTERVAL)


def looks_like_principal_id(s: Any) -> bool:
    """是否符合 principalId 形态（注意：photoId 同形态，需结合上下文判定）。"""
    return bool(_RE_ID_FULL.match(str(s or "")))


def _home_url(pid: str) -> str:
    return f"https://www.kuaishou.com/profile/{pid}"


def _live_url(pid: str) -> str:
    return f"https://live.kuaishou.com/u/{pid}"


def _extract_author(html: str) -> Optional[Dict[str, str]]:
    """从 SSR HTML 抽作者 ``{id, name}``（Fail Soft，抽不到返回 None）。

    优先用「id 紧邻 name」的作者对象结构 —— 这是实测最可靠的特征；
    退而求其次用显式 ``principalId`` 字段；最后才用主页链接。
    """
    if not html:
        return None
    m = _RE_AUTHOR_OBJ.search(html)
    if m:
        return {"id": m.group(1), "name": (m.group(2) or "").strip()}
    m = _RE_PRINCIPAL_FIELD.search(html) or _RE_PROFILE.search(html)
    if m:
        return {"id": m.group(1), "name": ""}
    return None


# ==========================================================================
# 策略（按 cost 升序执行：0=纯本地推断，1=一次请求，2=多次请求/低成功率）
# ==========================================================================
class InputShapeStrategy(ResolveStrategy):
    """cost 0 —— 输入本身就是 principalId（形态匹配），零成本直接采信。"""

    name = "input_shape"
    source = IdentitySource.INPUT
    cost = 0
    confidence = 0.9

    def applies(self, q: IdentityQuery) -> bool:
        return q.kind == IdentityKind.PRINCIPAL_ID

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        pid = q.raw
        if not looks_like_principal_id(pid):
            return None
        return self.build(principal_id=pid, home_url=_home_url(pid),
                          room_id=q.hint("room_id") or pid)


class UrlPathStrategy(ResolveStrategy):
    """cost 0 —— 输入是主页/直播链接，principalId 直接躺在路径里。"""

    name = "url_path"
    source = IdentitySource.HOME_URL
    cost = 0
    confidence = 0.95

    def applies(self, q: IdentityQuery) -> bool:
        return q.kind in (IdentityKind.HOME_URL, IdentityKind.LIVE_URL)

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        m = _RE_PROFILE.search(q.raw)
        if m:
            pid = m.group(1)
            return self.build(principal_id=pid, home_url=q.raw, room_id=pid)
        m = _RE_LIVE_U.search(q.raw)
        if m:
            pid = m.group(1)
            ident = self.build(principal_id=pid, home_url=_home_url(pid),
                               room_id=pid, live_id=pid)
            ident.identity_source = IdentitySource.LIVE_URL
            return ident
        return None


class ShareRedirectStrategy(ResolveStrategy):
    """cost 1 —— 分享短链 302 跳转，终链 query 里的 ``userId`` 就是 principalId。

    这是任务三实证的核心结论（~98% 置信）：**分享链接 userId == graphql principalId**。
    """

    name = "share_redirect"
    source = IdentitySource.SHARE_REDIRECT
    cost = 1
    confidence = 0.98

    def applies(self, q: IdentityQuery) -> bool:
        return bool(self._share_url(q))

    @staticmethod
    def _share_url(q: IdentityQuery) -> str:
        if q.kind == IdentityKind.SHARE_URL:
            return q.raw
        return q.hint("share_url")

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        url = self._share_url(q)
        if not url:
            return None
        resp = ctx.get(url, headers=_KS_HEADERS)
        # 终链（跟随重定向后）才带 userId；原始短链不带
        for candidate in (resp.url, url):
            pid = self._pid_from_url(candidate)
            if pid:
                return self.build(principal_id=pid, share_user_id=pid,
                                  share_url=url, home_url=_home_url(pid))
        # 终链没参数时，退而看落地页 SSR
        author = _extract_author(resp.text)
        if author:
            ident = self.build(principal_id=author["id"], share_url=url,
                               nickname=author.get("name", ""),
                               home_url=_home_url(author["id"]))
            ident.identity_source = IdentitySource.PAGE_SSR
            ident.confidence = 0.9
            return ident
        return None

    @staticmethod
    def _pid_from_url(url: str) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:  # noqa: BLE001
            return ""
        for key in ("userId", "authorId", "principalId"):
            for v in qs.get(key, []):
                if looks_like_principal_id(v):
                    return v
        return ""


class PostPageStrategy(ResolveStrategy):
    """cost 1 —— 作品页 SSR 含作者对象 ``{"id":"3x...","name":"..."}``。

    输入是作品链接，或 config 给了 ``seed_url``（该账号任意一条作品的链接）。
    额外收益：能顺带拿到**昵称**，让 config 不必手填 name。
    """

    name = "post_page_ssr"
    source = IdentitySource.PAGE_SSR
    cost = 1
    confidence = 0.95

    def applies(self, q: IdentityQuery) -> bool:
        return bool(self._page_url(q))

    @staticmethod
    def _page_url(q: IdentityQuery) -> str:
        if q.kind == IdentityKind.POST_URL:
            return q.raw
        seed = q.hint("seed_url") or q.hint("post_url")
        if seed:
            return seed
        pid = q.hint("photo_id")
        return f"https://v.m.chenzhongtech.com/fw/photo/{pid}" if pid else ""

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        url = self._page_url(q)
        if not url:
            return None
        resp = ctx.get(url, headers=_KS_HEADERS)
        author = _extract_author(resp.text)
        if not author:
            # 作品被删/审核中会返回通用页（无作者对象），这是种子本身的问题，不是解析 bug
            logger.debug("[kuaishou-identity] 作品页无作者对象（种子可能已失效）: %s", url)
            return None
        pid = author["id"]
        return self.build(principal_id=pid, nickname=author.get("name", ""),
                          home_url=_home_url(pid), extra={"seed_url": url})


class LiveProfileStrategy(ResolveStrategy):
    """cost 1 —— 直播页 ``live.kuaishou.com/u/<任意标识>`` 是**通用身份 oracle**。

    2026-08 实测的关键结论：该页 SSR 的 ``liveroom.playList[0].author`` **不是回显输入**，
    而是把输入反解成账号真身::

        输入 Sandy88888        → author={"id":"Sandy88888","name":"肥阿肥","originUserId":2117550}
        输入 3xrgxqkqp829xz6   → author={"id":"Sandy88888","name":"肥阿肥","originUserId":2117550}
                                          ^^^^^^^^^^^^ 同一个账号，字段完全一致

    也就是说 ``author.id`` 是账号的**规范标识**（用户设过快手号就是快手号，没设才是
    principalId），``originUserId`` 是不可变的数字真身。由此得到两个能力：

    1. **免费拿到 nickname / originUserId / living** —— 开播提醒只要 username 就够了；
    2. **交叉校验**：两个不同输入若 ``originUserId`` 相同，即可判定指向同一账号
       （见 :meth:`KuaishouIdentityResolver.verify`），无需任何硬编码映射。

    注意本策略**可能解不出 principal_id**（当账号设了快手号时 ``author.id`` 是快手号），
    这时它只补充别名字段，把主键留给其它策略 —— 这正是 merge「只填空位」的用意。
    """

    name = "live_profile"
    source = IdentitySource.PAGE_SSR
    cost = 1
    confidence = 0.9

    def applies(self, q: IdentityQuery) -> bool:
        return bool(self._probe_id(q))

    @staticmethod
    def _probe_id(q: IdentityQuery) -> str:
        """挑一个能喂给直播页的标识。"""
        if q.kind in (IdentityKind.PRINCIPAL_ID, IdentityKind.UNIQUE_NAME,
                      IdentityKind.ROOM_ID, IdentityKind.NICKNAME,
                      IdentityKind.UNKNOWN):
            if q.raw and "/" not in q.raw:
                return q.raw
        return q.hint("principal_id") or q.hint("unique_name") or q.hint("room_id")

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        probe = self._probe_id(q)
        if not probe:
            return None
        author = fetch_live_author(probe, ctx)
        if not author:
            return None
        canonical = str(author.get("id") or "")
        nickname = str(author.get("name") or "")
        origin = author.get("originUserId")
        ident = self.build(nickname=nickname, room_id=probe)
        if looks_like_principal_id(canonical):
            # 账号没设快手号 → 规范标识就是 principalId，直接采信
            ident.principal_id = canonical
            ident.home_url = _home_url(canonical)
            ident.live_id = canonical
        else:
            # 设过快手号 → canonical 是 unique_name，principalId 得靠别的证据
            ident.unique_name = canonical
        if origin not in (None, ""):
            ident.extra["origin_user_id"] = str(origin)
        ident.extra["living"] = bool(author.get("living"))
        return ident


class NicknameSearchStrategy(ResolveStrategy):
    """cost 2 —— 昵称搜索（**云 IP 下基本必失败**，仅作锦上添花）。

    快手所有 graphql（含 ``visionSearchUser``）从数据中心 IP 一律返回
    ``result=400002`` 验证码挑战。保留该策略是为了：登录 Cookie 可用时它能生效，
    以及让「用户只给了昵称」这条路在架构上是通的。失败即静默返回 None。
    """

    name = "nickname_search"
    source = IdentitySource.SEARCH
    cost = 2
    confidence = 0.6

    def applies(self, q: IdentityQuery) -> bool:
        return q.kind == IdentityKind.NICKNAME or bool(q.hint("nickname"))

    def run(self, q: IdentityQuery, ctx: ResolveContext) -> Optional[PrincipalIdentity]:
        keyword = q.raw if q.kind == IdentityKind.NICKNAME else q.hint("nickname")
        if not keyword:
            return None
        finder = getattr(ctx, "search_user", None)
        if not callable(finder):
            # 未注入搜索能力（默认情况）→ 不做无谓请求
            return None
        hit = finder(keyword)
        if not isinstance(hit, dict) or not looks_like_principal_id(hit.get("id")):
            return None
        pid = str(hit["id"])
        return self.build(principal_id=pid, nickname=str(hit.get("name") or keyword),
                          home_url=_home_url(pid))


# ==========================================================================
# 直播页 author 提取（身份 oracle + 开播状态，两处共用）
# ==========================================================================
class LiveProbeStatus:
    """直播页探测结果的判定，用于区分「拿不到」的几种原因。

    把这三者混为一谈会直接导致误判：限流当成查无此人 → 好账号被判死；
    查无此人当成限流 → 明知错的配置被无限重试还不报警。
    """

    OK = "ok"                    # 拿到 author
    RATE_LIMITED = "rate_limited"  # 被风控限流，值得退避重试
    NOT_FOUND = "not_found"      # 页面明说没这个人/这个直播间
    UNAVAILABLE = "unavailable"  # 网络失败、解析失败等说不清的情况


#: 快手 SSR ``playList[0].errorType.type`` 的已知取值（2026-08 实测）。
#: type=2 伴随 title「请求过快，请稍后重试」，是 IP 级限流。
_ET_RATE_LIMITED = (2,)
_RATE_LIMIT_HINTS = ("请求过快", "稍后重试", "频繁")


@dataclass
class LiveProbe:
    """一次直播页探测的结构化结果。"""

    status: str = LiveProbeStatus.UNAVAILABLE
    author: Optional[Dict[str, Any]] = None
    error_type: Optional[int] = None
    error_title: str = ""
    http_status: int = 0

    @property
    def ok(self) -> bool:
        return self.status == LiveProbeStatus.OK and bool(self.author)

    @property
    def gated(self) -> bool:
        return self.status == LiveProbeStatus.RATE_LIMITED


def probe_live_author(ident: str, ctx: ResolveContext,
                      retries: int = 2, backoff: float = 2.0) -> LiveProbe:
    """探测 ``live.kuaishou.com/u/<ident>``，返回结构化结果（Fail Soft，不抛）。

    快手限流不走 HTTP 状态码：它回 200 + 一个完整的壳页面，把真相写在
    ``playList[0].errorType``（``{"type":2,"title":"请求过快，请稍后重试"}``）。
    只看 HTTP 状态或只判 author 是否为空，都会把限流误读成「查无此人」。
    """
    if not ident:
        return LiveProbe(status=LiveProbeStatus.UNAVAILABLE)
    url = _live_url(ident)
    # 节流只针对真实网络；注入 fetcher（测试/其它数据源）不需要，也不该被拖慢
    paced = ctx.fetch is http_fetch
    last = LiveProbe(status=LiveProbeStatus.UNAVAILABLE)
    for attempt in range(max(1, retries + 1)):
        if paced:
            _pacer.wait()
        resp = ctx.get(url, headers=_KS_HEADERS)
        last = _probe_from_response(resp)
        if last.ok:
            return last
        # 只有「限流」和「说不清」值得再试；明确查无此人就别浪费请求了
        retryable = last.gated or (
            last.status == LiveProbeStatus.UNAVAILABLE
            and resp.status in _RETRYABLE_STATUS
        )
        if not retryable:
            break
        if attempt < retries and paced:
            _pacer.penalize(backoff * (attempt + 1))
            time.sleep(backoff * (attempt + 1))
    return last


def _probe_from_response(resp: Any) -> LiveProbe:
    """把 HTTP 响应解读成 LiveProbe（纯函数，便于单测）。"""
    http_status = int(getattr(resp, "status", 0) or 0)
    if not getattr(resp, "ok", False):
        return LiveProbe(status=LiveProbeStatus.UNAVAILABLE, http_status=http_status)
    author = author_from_live_html(getattr(resp, "text", "") or "")
    if author:
        return LiveProbe(status=LiveProbeStatus.OK, author=author, http_status=http_status)
    et = error_type_from_live_html(getattr(resp, "text", "") or "")
    if not et:
        return LiveProbe(status=LiveProbeStatus.UNAVAILABLE, http_status=http_status)
    etype = et.get("type")
    title = str(et.get("title") or "")
    etype_i = etype if isinstance(etype, int) else None
    is_limited = (etype_i in _ET_RATE_LIMITED) or any(h in title for h in _RATE_LIMIT_HINTS)
    return LiveProbe(
        status=LiveProbeStatus.RATE_LIMITED if is_limited else LiveProbeStatus.NOT_FOUND,
        error_type=etype_i, error_title=title, http_status=http_status,
    )


def fetch_live_author(ident: str, ctx: ResolveContext,
                      retries: int = 2, backoff: float = 2.0) -> Optional[Dict[str, Any]]:
    """取直播页 author 对象；拿不到返回 None（:func:`probe_live_author` 的薄封装）。"""
    return probe_live_author(ident, ctx, retries=retries, backoff=backoff).author


def _live_play_entry(html: str) -> Optional[Dict[str, Any]]:
    """取 SSR 里的 ``liveroom.playList[0]``（author 与 errorType 都在这）。"""
    from backend.adapters.kuaishou import _extract_initial_state  # 避免循环导入

    state = _extract_initial_state(html)
    if not isinstance(state, dict):
        return None
    play_list = ((state.get("liveroom") or {}).get("playList")) or []
    if not play_list:
        return None
    first = play_list[0]
    return first if isinstance(first, dict) else None


def author_from_live_html(html: str) -> Optional[Dict[str, Any]]:
    """从直播页 HTML 抽 ``playList[0].author``（纯函数，便于单测）。"""
    author = (_live_play_entry(html) or {}).get("author")
    return author if isinstance(author, dict) and author else None


def error_type_from_live_html(html: str) -> Optional[Dict[str, Any]]:
    """从直播页 HTML 抽 ``playList[0].errorType`` —— 快手把风控原因写在这。"""
    et = (_live_play_entry(html) or {}).get("errorType")
    return et if isinstance(et, dict) and et else None


# ==========================================================================
# Resolver
# ==========================================================================
class KuaishouIdentityResolver(IdentityResolver):
    """快手身份解析器。

    策略执行顺序（基类按 cost 排序）::

        cost0  input_shape    输入就是 principalId
        cost0  url_path       主页/直播链接路径里的 principalId
        cost1  share_redirect 分享短链 302 终链 ?userId=      ← 任务三实证主链路
        cost1  post_page_ssr  作品页 SSR 作者对象
        cost1  live_profile   直播页 author（昵称/originUserId/开播态，可能含 principalId）
        cost2  nickname_search 昵称搜索（云 IP 基本被风控，需 Cookie）

    **已知能力边界**（2026-08 实测，写在这里避免后人重复踩）：
    只给「用户名」（如 ``Sandy88888``）时，匿名云 IP 下**没有任何** SSR 端点能正向解出
    principalId —— PC 主页/移动主页/移动直播页/短视频页均已逐一验证失败。此时 resolver
    仍会返回昵称与 originUserId（够开播提醒用），但 ``principal_id`` 为空，
    新作品监控需要用户补一个 ``share_url`` 或任意一条作品链接 ``seed_url``。
    """

    platform = "kuaishou"

    def __init__(self, *args: Any, verify_identity: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: 是否做 originUserId 交叉校验（多一次请求，换取「解错人」不会发生）
        self.verify_identity = bool(verify_identity)

    def strategies(self) -> List[ResolveStrategy]:
        return [
            InputShapeStrategy(),
            UrlPathStrategy(),
            ShareRedirectStrategy(),
            PostPageStrategy(),
            LiveProfileStrategy(),
            NicknameSearchStrategy(),
        ]

    def classify(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return IdentityKind.UNKNOWN
        if _RE_LIVE_U.search(raw):
            return IdentityKind.LIVE_URL
        if _RE_PROFILE.search(raw):
            return IdentityKind.HOME_URL
        if _RE_PHOTO.search(raw) or "/short-video/" in raw:
            return IdentityKind.POST_URL
        if _RE_SHORT.search(raw):
            return IdentityKind.SHARE_URL
        if raw.startswith("http"):
            return IdentityKind.UNKNOWN
        if looks_like_principal_id(raw):
            return IdentityKind.PRINCIPAL_ID
        # 快手号规则：字母开头的字母数字下划线串；其余（含中文）当昵称
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,}", raw):
            return IdentityKind.UNIQUE_NAME
        return IdentityKind.NICKNAME

    def verify(self, ident: PrincipalIdentity, ctx: ResolveContext) -> str:
        """校验 + 富化，一次请求两用。

        **校验**：解出的 principalId 必须和输入指向同一账号（比对 ``originUserId``）。
        这是防「解错人」的最后一道闸 —— 曾经出过「随机抓到推荐流里的别人」的事故。
        两边都拿得到真身 id 才比对；拿不到就放行（Fail Soft：校验请求失败 ≠ 身份错误，
        不该因为一次网络抖动就把好账号判死）。

        **富化**：同一次请求顺带补齐 nickname / unique_name / 开播态，
        这样 config 里用户只填一个 id 也能在通知里显示人话昵称。
        """
        expect = str((ident.extra or {}).get("origin_user_id") or "")
        if self.verify_identity and not expect:
            expect = self._origin_from_input(ident, ctx)

        probe = probe_live_author(ident.principal_id, ctx)
        if probe.gated:
            # 被限流：这次校验根本没做成，和「身份错」是两回事。记 UNKNOWN 让它下轮重验，
            # 顺便把风控现场留在 extra 里，方便回答「为什么一直是未校验」。
            ident.extra["verify_blocked_by"] = probe.error_title or "rate_limited"
            return VerifyOutcome.UNKNOWN
        if probe.status == LiveProbeStatus.NOT_FOUND and self.verify_identity:
            # 页面明确说没这个人 —— 不是网络问题，是身份真的不对
            logger.warning("[kuaishou-identity] principal_id=%s 在直播页查无此人（%s），丢弃",
                           ident.principal_id, probe.error_title or probe.error_type)
            return VerifyOutcome.FAIL
        author = probe.author
        if not author:
            # 校验请求没做成 ≠ 身份错误，但也绝不能当成验过（否则「配错人」会蒙混过关）
            return VerifyOutcome.UNKNOWN

        got = str(author.get("originUserId") or "")
        canonical = str(author.get("id") or "")

        # ---- 富化（无论校验与否都做）----
        if not ident.nickname:
            ident.nickname = str(author.get("name") or "")
        if canonical and not looks_like_principal_id(canonical) and not ident.unique_name:
            ident.unique_name = canonical
        if got:
            ident.extra["origin_user_id"] = got
        ident.extra["living"] = bool(author.get("living"))
        if not ident.home_url:
            ident.home_url = _home_url(ident.principal_id)

        # ---- 校验 ----
        if not self.verify_identity:
            return VerifyOutcome.PASS
        if not got:
            return VerifyOutcome.UNKNOWN
        if not expect:
            # 输入侧拿不到独立的真身 id（如输入本身就是 principalId / 分享链接）：
            # 账号存在性已被这次请求证实，但没有第二方证据可比对 → 记未校验。
            return VerifyOutcome.UNKNOWN
        if got != expect:
            logger.warning(
                "[kuaishou-identity] 身份校验不通过：principal_id=%s 的 originUserId=%s，"
                "但输入指向 %s —— 判定为解错人，丢弃",
                ident.principal_id, got, expect,
            )
            return VerifyOutcome.FAIL
        ident.confidence = 1.0
        ident.extra["verified_by"] = "origin_user_id"
        return VerifyOutcome.PASS

    @staticmethod
    def _origin_from_input(ident: PrincipalIdentity, ctx: ResolveContext) -> str:
        """输入侧还没测过 originUserId 时补一次 —— 只在「输入是另一个独立标识」时才值得。

        输入本身就是 principalId 的话，拿它去比对自己毫无意义（自证不算证），
        这里直接跳过省一次请求。
        """
        q = ctx.query
        if q is None:
            return ""
        probe = q.raw if (q.raw and "/" not in q.raw) else q.hint("unique_name")
        if not probe or probe == ident.principal_id:
            return ""
        author = fetch_live_author(probe, ctx)
        return str((author or {}).get("originUserId") or "")
