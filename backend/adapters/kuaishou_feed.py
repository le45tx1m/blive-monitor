"""快手作品流抓取：``live_api/profile/public`` 通道（免登录 Cookie）。

背景 —— 为什么是这条通道
========================
此前快手新作监控走 ``www.kuaishou.com/graphql`` 的 ``visionProfilePhotoList``，
实测**该端点已被前端弃用**（打开 profile 页，页面自身发出的 graphql 请求数为 0），
裸请求恒返回 ``result=2``；换 H5 分享域的 ``rest/wd/feed/profile`` 则恒返回
``result=2001``（滑块验证码）。以下组合已全部实测被挡，且与出口 IP 无关
（沙箱 + GitHub Actions 的 Azure IP 双环境、微信/copylink/短链三渠道、
kpfdrx3x/c.kuaishou/v.m.chenzhongtech 三子域、两个账号，结论一致）：

======================================  ==================================
通道                                     裸请求结果
======================================  ==================================
``www.kuaishou.com/graphql``            ``result=2``；带自造 did 反而升级为
                                        ``400002``（验证码挑战）
``v.m.chenzhongtech.com/rest/wd/...``   ``result=2001``（滑块）
``live.kuaishou.com/m_graphql``         ``result=400010``（频控）
``live_api/profile/public`` 裸请求       ``result=2``、``list=[]``
======================================  ==================================

**真正可行的是第五条**：用浏览器先访问 ``www.kuaishou.com`` 主站让风控 JS 种下
``kwfv1``/``kwssectoken``/``kwscode`` 等 token，再打开
``live.kuaishou.com/profile/<principalId>``，拦截**页面自己发出**的
``live_api/profile/public`` 响应 —— 该请求带 JS 现算的 ``__NS_hxfalcon`` 签名与
新鲜 cookie，服务端返回 ``result=1`` + 真实作品列表。这与 RSSHub 的
``lib/routes/kuaishou/profile.ts`` 是同一思路（公共服务不可能要求用户 Cookie，
其做法反向印证了免登录路径的存在），也与本项目抖音 ``get_latest_aweme`` 的
「浏览器打开分享页 + 拦截 XHR」范式一致。

三个实测得出、务必保留的细节
============================
1. **首次请求几乎必然 ``result=2``**，需要重新导航若干次才转 ``result=1``
   （实测 4~5 次）。但**同一 context 预热成功后，后续账号第 1 次导航即命中** ——
   所以浏览器上下文必须跨账号复用，见 :class:`KuaishouFeedSession`。
2. **不能手动 fetch**：在页面上下文里 ``fetch()`` 同一 URL 连打 12 次全是
   ``result=2``（缺 JS 现算签名）。必须由页面自身发起。
3. **列表不是按时间倒序** —— 前几条是置顶作品。实测某账号 list[0] 是
   2025-11-05、list[3] 才是最新的 2026-08-07。**取 list[0] 当最新是错的**，
   必须按真实发布时间排序（时间来源见 :func:`kuaishou_feed_core.decode_media_meta`）。

游客身份（did）必须稳定复用，否则永远被当「新访客」
====================================================
快手 visitor JS 给每个**全新浏览器上下文**都生成新的 ``did``（游客/设备 ID，
``web_<hex>``）。匿名云 IP 下，每次都像「第一次来的新访客」→ 风控权重更高、
更容易被 gated（``result=2`` / ``400002``）—— 这正是「快手还是风控」的根因之一。

对策见 :class:`KuaishouFeedSession` 与模块顶部的游客身份缓存：
* **首次成功预热**时抓出 ``did`` 等「设备/配置类」身份 cookie（``_GUEST_IDENTITY_COOKIE_NAMES``），
  落盘到 ``config/kuaishou_guest_visitor.json`` 并随状态文件一起被 CI 提交；
* 之后**每个新 context**（包括跨 CI 运行）都注入**同一个 did**，让快手认作「同一游客」，
  逐步积累正常访客信誉，压低 gated 率。
* **风控 token**（``kwfv1``/``kwssectoken``/``kwscode``）**不跨运行复用** —— 它们次数/时效
  受限，每次预热让 visitor JS 现算刷新，避免复用一个已被打废的旧 token 反而更可疑。

调试/起号时可调 :func:`reset_guest_visitor_cache` 清掉缓存，下轮重新养一个全新 did。

风控 token 是「次数/时效受限」的（打废）
======================================
实测浏览器养熟的 token 不是无限耐用：导出后裸 HTTP 复用立刻 ``result=2``；
即便在浏览器内，连续命中若干次后也会退回 ``result=2``（**前 ~4 次成功、之后全废**）。
所以 :class:`KuaishouFeedSession` 两层兜底：
* **主动**：每成功 ``MAX_USES_PER_TOKEN`` 次，下一个账号开始前重新预热（刷新 token），
  避免撞上耗尽；
* **被动（退化自愈）**：整轮导航若全是 ``result=2``/``400002``，强制重预热再来一轮。
两层都保证「token 打废」不会让后续账号静默失败。

响应字段的坑
============
条目里**没有 ``timestamp``，也没有 ``caption``**，可用字段只有
``id/poster/playUrl/imgUrls/workType/counts/author`` 等。发布时间靠
:func:`kuaishou_feed_core.decode_media_meta` 从 CDN URL 反解，文案由调用方按需补取
详情页标题。

> 纯逻辑（解析 / 反解 / 校验 / 文案清洗）已抽到 :mod:`backend.adapters.kuaishou_feed_core`，
> 本模块只负责浏览器会话与预热；这样单测无需启动 Chromium。
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

# 纯逻辑全部来自 kuaishou_feed_core（无浏览器依赖、可单测）；此处 re-export 以保持
# backend.adapters.kuaishou / tools/kuaishou_probe 的 import 路径不变。
from backend.adapters.kuaishou_feed_core import (  # noqa: F401
    ANTIBOT_COOKIES,
    PHOTO_URL_TMPL,
    PROFILE_PUBLIC_PATH,
    PROFILE_URL_TMPL,
    WARMUP_URL,
    clean_caption,
    decode_media_meta,
    normalize_item,
    parse_profile_public,
    photo_url,
    pick_latest,
    sort_by_time,
    verify_ownership,
)

logger = __import__("logging").getLogger(__name__)


# ==================== 稳定游客身份缓存（跨 context / 跨运行复用 did） ====================
#
# 风控根因：快手 visitor JS 给每个**全新 context** 都生成新的 ``did``（游客/设备 ID，
# 形如 ``web_<hex>``）。匿名云 IP 下，每次都像「第一次来的新访客」→ 风控权重更高、
# 更容易被 gated（result=2 / 400002）。本模块在**首次成功预热**时把游客身份
# cookie（did 等）抓出来，后续每个 context 注入**同一个 did**，让快手认作「同一游客」
# 而非每次新访客，从而降低 gated 率。
#
# 只缓存「设备/配置类」身份 cookie，**绝不**跨运行复用 ``kwfv1/kwssectoken/kwscode``
# 这类次数/时效受限的风控 token —— 它们每次预热都让 visitor JS 现算刷新，避免复用一个
# 已被打废的旧 token 反而更可疑。
#
# 缓存同时落盘到仓库（见 CI 的 PERSIST_FILES），于是**跨 CI 运行**也复用同一 did，
# 让这个匿名设备逐步积累「正常访客」信誉，是压低产线 gated 率的关键杠杆。
_GUEST_IDENTITY_COOKIE_NAMES = (
    "did", "kpf", "kpn", "clientid", "didv",
    "ktrace-context", "kwpsecproductname",
)

# 缓存文件路径：仓库根目录，与 state.json 等状态文件并列。必须放在根目录（而非
# config/ 子目录）—— CI 的 Persist 步骤会 `git reset --hard origin/master` 再 `git
# add` 状态文件，子目录文件走 TMPD 备份/恢复时会丢前缀无法还原；根目录文件则被
# STATE_FILES / PERSIST_FILES 一并备份提交，从而跨 CI 运行保留同一 did。
_GUEST_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "kuaishou_guest_visitor.json",
)

_GUEST_CACHE_LOCK = threading.Lock()
_GUEST_DID_CACHE: Optional[str] = None
_GUEST_VISITOR_COOKIES: List[Dict[str, Any]] = []


def _load_guest_visitor_cache() -> None:
    """启动时从磁盘读回上一轮捕获的游客身份（did 等），用于跨运行复用。"""
    global _GUEST_DID_CACHE, _GUEST_VISITOR_COOKIES
    try:
        if not os.path.exists(_GUEST_CACHE_FILE):
            return
        with open(_GUEST_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("cookies"), list):
            _GUEST_VISITOR_COOKIES = [c for c in data["cookies"] if isinstance(c, dict)]
        _GUEST_DID_CACHE = data.get("did") or None
        if _GUEST_DID_CACHE:
            logger.info("[kuaishou] 载入缓存游客身份 did=%s（跨运行复用）",
                        _GUEST_DID_CACHE[:14] + "…")
    except Exception as e:  # noqa: BLE001
        logger.debug("[kuaishou] 读取游客身份缓存失败（忽略，本轮重新养）: %s", e)


def _save_guest_visitor_cache() -> None:
    """把当前游客身份（did 等）落盘，供后续运行/context 复用。失败静默忽略。"""
    try:
        d = os.path.dirname(_GUEST_CACHE_FILE)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with _GUEST_CACHE_LOCK:
            payload = {"did": _GUEST_DID_CACHE,
                       "cookies": _GUEST_VISITOR_COOKIES}
        with open(_GUEST_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as e:  # noqa: BLE001
        logger.debug("[kuaishou] 写入游客身份缓存失败（忽略）: %s", e)


def reset_guest_visitor_cache() -> None:
    """清空游客身份缓存（调试/起号用）。下次预热会重新养一个全新 did。"""
    global _GUEST_DID_CACHE, _GUEST_VISITOR_COOKIES
    with _GUEST_CACHE_LOCK:
        _GUEST_DID_CACHE = None
        _GUEST_VISITOR_COOKIES = []
    try:
        if os.path.exists(_GUEST_CACHE_FILE):
            os.remove(_GUEST_CACHE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _norm_kuaishou_domain(domain: str) -> str:
    """把身份 cookie 的 domain 归一化到 ``.kuaishou.com``，确保 www/live 等子域都可见。"""
    d = (domain or "").lower()
    if d.endswith("kuaishou.com"):
        return ".kuaishou.com"
    return domain or ".kuaishou.com"


# 进程启动即尝试读回上一轮缓存（无文件/无权限时静默跳过）。
_load_guest_visitor_cache()


def _parse_cookie_string(cookie_str: str, domain: str) -> List[Dict[str, str]]:
    """把 ``"k=v; k2=v2"`` 拆成 Playwright ``add_cookies`` 需要的 dict 列表。

    仅做字符串拆分，不依赖浏览器；供 :meth:`KuaishouFeedSession._apply_kuaishou_cookie`
    复用，``domain`` 固定为 ``.kuaishou.com``（覆盖 www/live 等子域）。

    重名去重（保留最后一条）：用户从多个请求合并复制 Cookie 时，``kwscode`` /
    ``kwssectoken`` / ``kwpsecproductname`` 等字段常出现两次且值不同。同名同域的两条
    cookie 会触发 Playwright ``add_cookies`` 的重复校验报错，这里按 name 去重、
    保留后出现的（通常是较新/较全的那条）。
    """
    out: List[Dict[str, str]] = []
    seen: Dict[str, Dict[str, str]] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k in seen:
            # 重名：保留最后一条的值（位置不变，避免打乱其它 cookie 顺序）
            seen[k]["value"] = v
        else:
            item = {"name": k, "value": v, "domain": domain, "path": "/"}
            seen[k] = item
            out.append(item)
    return out


# ==================== 浏览器会话 ====================

class KuaishouFeedSession:
    """快手作品流的浏览器会话：**一次预热，全轮复用；token 打废则主动重预热**。

    为什么要有这个类而不是每个账号开一次浏览器：预热代价是不对称的 ——
    实测冷启动首个账号要重新导航 4~5 次才等到 ``result=1``，而同一个 context
    热起来之后，**下一个账号第 1 次导航就命中**。每账号各建一个 context 等于
    每个都付一遍冷启动，还会因短时间高频打同一批接口更快撞上风控。

    用法::

        sess = KuaishouFeedSession(playwright_context)
        parsed = sess.fetch("3x7ju263tgi5dn9")   # -> parse_profile_public 结果
        ...
        sess.close()

    Fail Soft：任何异常都不向上抛浏览器细节，取不到就返回 ``ok=False``，
    由适配器决定记 gated 还是跳过。
    """

    #: 单账号最多重新导航几次（冷启动实测 4~5 次，留些余量）
    MAX_NAV = 7
    #: 每次导航后等响应的秒数（仅作兜底退避用，主等待已改为精确等接口响应）
    WAIT_SEC = 7
    #: 导航超时（毫秒）
    NAV_TIMEOUT_MS = 45000
    #: 预热后轮询等到游客 cookie（did + 风控 token）种下的最长毫秒数。
    #: 取代旧版 ``networkidle``（快手 SPA 永不 idle，旧版必等满 45s 才降级，单轮 ~54s）。
    #: 实测 ``domcontentloaded`` + 轮询约 6~9s 即可等到 did 与 kwfv1/kwssectoken/kwscode 全套种下。
    VISITOR_WAIT_MS = 10000
    #: 轮询间隔（毫秒）
    VISITOR_POLL_MS = 400
    #: 预热最多重试几次（token 没种下就重导航主站，直到拿到或耗尽次数）
    MAX_WARMUP_RETRY = 3
    #: 同一 token 连续成功抓取多少次后主动重预热（打废自愈，实测配额 ~4）。
    #: 设为 0 可关闭主动重预热，仅保留「整轮全 result=2 强制重预热」的被动兜底。
    MAX_USES_PER_TOKEN = 4

    def __init__(self, browser_context: Any, user_agent: str = "",
                 kuaishou_cookie: str = "") -> None:
        self._src = browser_context
        self._ua = user_agent
        self._ctx = None
        self._warmed = False
        self._uses = 0
        # 可选：登录 Cookie（KUAISHOU_COOKIE），注入自建隔离 context 以突破匿名风控。
        # 空串 = 走免 Cookie 匿名通道（live_api/profile/public + 预热种 token）。
        self._kuaishou_cookie = kuaishou_cookie or ""

    # ---- 生命周期 ----
    def _ensure_ctx(self):
        """懒建专用 context（与调用方的抖音 context 隔离，避免 UA/cookie 串味）。"""
        if self._ctx is not None:
            return self._ctx
        browser = getattr(self._src, "browser", None)
        if browser is not None:
            kw = {"viewport": {"width": 1366, "height": 900}, "locale": "zh-CN"}
            if self._ua:
                kw["user_agent"] = self._ua
            self._ctx = browser.new_context(**kw)
        else:
            # 拿不到 browser 就直接用传进来的 context（测试替身/降级路径）
            self._ctx = self._src
        # 登录 Cookie（KUAISHOU_COOKIE）优先：注入后可直接突破匿名风控。
        # 否则走免 Cookie 匿名通道：先注入上一轮捕获的**稳定游客身份**（did 等），
        # 让快手认作「同一游客」而非每次新访客（降低 gated），风控 token 仍由 visitor
        # JS 本次现算刷新（见 _warmup）。
        if self._kuaishou_cookie:
            self._apply_kuaishou_cookie(self._ctx)
        else:
            self._apply_visitor_cookies(self._ctx)
        return self._ctx

    def _apply_kuaishou_cookie(self, ctx) -> None:
        """把 KUAISHOU_COOKIE 拆条写入隔离 context（仅在配置了时调用）。"""
        cookies = _parse_cookie_string(self._kuaishou_cookie, ".kuaishou.com")
        if not cookies:
            return
        try:
            ctx.add_cookies(cookies)
            logger.info("[kuaishou] 已注入登录 Cookie（%d 条），可突破作品接口风控", len(cookies))
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 注入快手 Cookie 失败: %s", e)

    def _apply_visitor_cookies(self, ctx) -> None:
        """匿名通道：把缓存的稳定游客身份 cookie（did 等）注入 context。

        仅注入「设备/配置类」身份 cookie（见 ``_GUEST_IDENTITY_COOKIE_NAMES``），
        不注入已被打废风险的风控 token。首次运行缓存为空时直接跳过，由 ``_warmup``
        现场养出 did 并捕获。
        """
        cookies = _GUEST_VISITOR_COOKIES
        if not cookies:
            return
        try:
            ctx.add_cookies(cookies)
            logger.info("[kuaishou] 注入稳定游客身份 cookie（%d 条，did=%s），复用同一访客降风控",
                        len(cookies), (_GUEST_DID_CACHE or "")[:14] + "…")
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 注入游客身份 cookie 失败: %s", e)

    def _capture_visitor_cookies(self, ctx) -> None:
        """预热成功后抓取游客身份 cookie（did 等）进模块缓存，供后续复用/落盘。"""
        global _GUEST_DID_CACHE, _GUEST_VISITOR_COOKIES
        try:
            all_cookies = ctx.cookies()
        except Exception:  # noqa: BLE001
            return
        picked = [
            {**c, "domain": _norm_kuaishou_domain(c.get("domain"))}
            for c in all_cookies
            if c.get("name") in _GUEST_IDENTITY_COOKIE_NAMES
            and ".kuaishou.com" in (c.get("domain") or "")
        ]
        if not picked:
            return
        # 合并进缓存（按 name 去重，保留最新值）
        by_name: Dict[str, Dict[str, Any]] = {}
        for c in list(_GUEST_VISITOR_COOKIES) + picked:
            by_name[c.get("name")] = c
        _GUEST_VISITOR_COOKIES = list(by_name.values())
        did = next((c["value"] for c in picked if c["name"] == "did"), None)
        if did:
            _GUEST_DID_CACHE = did
            logger.info("[kuaishou] 捕获稳定游客 did=%s（后续复用，降低风控）",
                        did[:14] + "…")
        _save_guest_visitor_cache()

    def _wait_visitor_cookies(self, page) -> bool:
        """轮询直到 visitor JS 真正种下 ``did`` + 风控 token（取代旧版 networkidle）。

        旧版 ``wait_until='networkidle'`` 在快手 SPA 上永远等不到，必等满 45s 超时后才
        降级 ``domcontentloaded``，单轮白烧 ~54s。这里 ``domcontentloaded`` 返回后，
        直接在 Python 侧轮询 ``context.cookies()``（不受 httpOnly 限制、比读
        ``document.cookie`` 更全），直到 ``did`` 与 ``ANTIBOT_COOKIES`` 都出现或超时。
        """
        deadline = time.monotonic() + self.VISITOR_WAIT_MS / 1000.0
        while time.monotonic() < deadline:
            names = {c.get("name") for c in page.context.cookies()}
            if "did" in names and (names & set(ANTIBOT_COOKIES)):
                return True
            page.wait_for_timeout(self.VISITOR_POLL_MS)
        return False

    def close(self) -> None:
        """关闭自建 context（借用外部 context 时不动它）。"""
        if self._ctx is not None and self._ctx is not self._src:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = None
        self._warmed = False
        self._uses = 0

    def _quota_exhausted(self) -> bool:
        """token 配额是否已用尽（用于主动重预热判断，便于单测）。"""
        return self.MAX_USES_PER_TOKEN > 0 and self._uses >= self.MAX_USES_PER_TOKEN

    def _warmup(self, page) -> bool:
        """访问主站跑完整 visitor JS，种下 ``did`` + 风控 token（``kwfv1``/``kwssectoken``/``kwscode``）。

        跳过这步的话，profile 页发出的请求会恒返回 ``result=2`` —— 这是整条
        链路唯一不可省的前置动作。

        **返回是否确认种下了游客身份**：之前「一次性置 ``_warmed=True``」的写法
        在 token 实际没种下时也会误以为已预热，导致整轮静默失败（表现为所有账号
        全 ``result=2``）。这里改成用 cookie 是否真实存在判定，并最多重试
        ``MAX_WARMUP_RETRY`` 次。

        关键改进（修复「快手还是风控」）：
        * 不再用 ``wait_until='networkidle'``（快手 SPA 永不 idle，旧版必等满 45s 超时
          才降级，单轮 ~54s），改为 ``domcontentloaded`` + :meth:`_wait_visitor_cookies`
          轮询，约 6~9s 即可确认 did 与风控 token 全套种下。
        * 预热成功后 :meth:`_capture_visitor_cookies` 抓出 did 等游客身份，供后续
          context 复用同一 did（见模块顶部缓存说明），避免每次被当「新访客」抬高风险权重。
        """
        if self._warmed:
            return True
        self._uses = 0  # 新预热 = token 配额重新计起
        planted = False
        last_err = ""
        for attempt in range(1, self.MAX_WARMUP_RETRY + 1):
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded",
                          timeout=self.NAV_TIMEOUT_MS)
                # 显式等到 visitor JS 真正种下 did + 风控 token（不再白等 networkidle 45s）
                self._wait_visitor_cookies(page)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                logger.warning("[kuaishou] 主站预热第 %d/%d 次失败: %s",
                               attempt, self.MAX_WARMUP_RETRY, e)
                # 主站打不开也别空耗重试：注入的登录 Cookie 里若已带风控 token，
                # 直接按已预热继续（2026-08 CI 实测海外出口 www.kuaishou.com 频繁
                # ERR_TIMED_OUT，3 次重试白烧 3+ 分钟，而带着注入 token 直接导航
                # profile 页仍有机会拿到 result=1）。
                names = {c.get("name") for c in page.context.cookies()}
                if names & set(ANTIBOT_COOKIES):
                    logger.info("[kuaishou] 主站不可达，但上下文已带风控 Cookie"
                                "（登录 Cookie 注入），按已预热继续")
                    planted = True
                    break
                continue
            names = {c.get("name") for c in page.context.cookies()}
            planted = ("did" in names) and bool(names & set(ANTIBOT_COOKIES))
            if planted:
                break
        if planted:
            # 抓出游客身份（did 等）进缓存，后续 context/运行复用同一访客
            self._capture_visitor_cookies(page.context)
        self._warmed = planted
        if not planted:
            logger.warning(
                "[kuaishou] 主站预热未种下游客身份（profile 接口将恒 result=2）"
                " last_err=%s cookies=%s",
                last_err, sorted({c.get("name") for c in page.context.cookies()}),
            )
        return planted

    # ---- 抓取 ----
    def _cycle(self, ctx, pid: str):
        """单次「预热 + 导航循环」。返回 ``(parsed, seen)``。

        ``parsed`` 命中作品列表时为 :func:`parse_profile_public` 的结果（``ok=True``），
        否则为 ``ok=False`` 的失败字典；``seen`` 是这一轮拦截到的 ``result`` 状态码序列，
        供上层判断是否要退化自愈（强制重预热）。
        """
        import json as _json

        page = ctx.new_page()
        best: Dict[str, Any] = {}
        seen: List[Any] = []

        def on_response(resp):
            nonlocal best, seen
            if PROFILE_PUBLIC_PATH not in resp.url:
                return
            try:
                body = resp.body().decode("utf-8", "replace")
                parsed = parse_profile_public(_json.loads(body))
            except Exception:  # noqa: BLE001 —— 单条响应解析失败不影响整轮
                return
            seen.append(parsed.get("result"))
            if parsed.get("ok") and not best:
                best = parsed

        try:
            page.on("response", on_response)
            self._warmup(page)
            url = PROFILE_URL_TMPL.format(pid=pid)
            for i in range(self.MAX_NAV):
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=self.NAV_TIMEOUT_MS)
                except Exception as e:  # noqa: BLE001
                    logger.debug("[kuaishou] %s 第 %d 次导航异常: %s", pid, i + 1, e)
                # XHR 可能在 goto 期间就已返回（handler 已设 best），先判一次避免傻等
                if best:
                    break
                # 精确等待页面自身发出的作品接口响应（比固定 sleep 更稳，也不会过早退出）
                try:
                    page.wait_for_response(
                        lambda r: PROFILE_PUBLIC_PATH in r.url, timeout=9000)
                except Exception:  # noqa: BLE001 —— 等不到就靠下面的退避再导航
                    pass
                if best:
                    break
                # result=2 是「预热还不够」/被风控卡住，退避后重新导航让页面重算签名
                page.wait_for_timeout(1200)
                if best:
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] %s 作品抓取异常: %s", pid, e)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

        # 兜底抓取游客身份（did 等）：profile 导航同样会触发 visitor JS 种下 did，
        # 而 _warmup 的判定只看 warmup 页的 cookie——若 did 是在 profile 页才种下的
        # （海外出口常见），_warmup 会判 planted=False 导致漏抓。这里从整轮结束后的
        # context 全量 cookie 再抓一次，确保无论 did 在哪个阶段种下都能被捕获复用。
        try:
            self._capture_visitor_cookies(ctx)
        except Exception:  # noqa: BLE001
            pass

        if best:
            return best, seen
        last = seen[-1] if seen else None
        return {"ok": False, "result": last, "items": [], "living": None,
                "author_name": "", "author_id": "",
                "detail": f"未拿到作品列表（响应序列={seen or '无'}）"}, seen

    def fetch(self, principal_id: str) -> Dict[str, Any]:  # noqa: C901
        """打开作者页、拦截页面自身的作品接口响应。

        Args:
            principal_id: 快手 principalId（如 ``3x7ju263tgi5dn9``）。

        Returns:
            :func:`parse_profile_public` 的结果；失败时 ``ok=False`` 且
            ``result`` 记录最后一次看到的状态码（2=预热不足，None=没拦到）。

        打废自愈（两层）：
        * **主动**：本次成功计入 ``_uses``，达到 ``MAX_USES_PER_TOKEN`` 后把
          ``_warmed`` 置 False，下一个账号开始前重新预热（刷新被打废的 token）。
        * **被动（退化自愈）**：整轮拦截到的全是 ``result=2`` / ``400002``（验证码挑战），
          说明预热没生效或被风控卡住，强制重预热再来一轮 —— 救回「token 种下了但首轮
          恰好没命中」的情况，也不至于在纯 IP 被标记时无限空转（最多两轮）。
        """
        pid = str(principal_id or "").strip()
        if not pid:
            return {"ok": False, "result": None, "items": [], "living": None,
                    "author_name": "", "author_id": "", "detail": "缺 principalId"}

        ctx = self._ensure_ctx()
        parsed, seen = self._cycle(ctx, pid)
        if parsed.get("ok"):
            self._uses += 1
            if self._quota_exhausted():
                # 主动重预热：下一个账号重新养 token，避免撞上打废
                logger.info("[kuaishou] %s 已达 token 配额(%d)，下个账号前重预热",
                            pid, self.MAX_USES_PER_TOKEN)
                self._warmed = False
            parsed["nav_count"] = len(seen)
            parsed["seen"] = seen
            return parsed

        only_blocked = seen and all(s in (2, 400002) for s in seen)
        if only_blocked:
            logger.info("[kuaishou] %s 首轮全 result=2/400002，强制重预热重试", pid)
            self._warmed = False
            parsed2, seen2 = self._cycle(ctx, pid)
            seen = seen + seen2
            if parsed2.get("ok"):
                self._uses += 1
                if self._quota_exhausted():
                    self._warmed = False
                parsed2["nav_count"] = len(seen)
                parsed2["seen"] = seen
                return parsed2
            parsed = parsed2

        parsed["nav_count"] = len(seen)
        parsed["seen"] = seen
        parsed["detail"] = f"未拿到作品列表（响应序列={seen or '无'}）"
        return parsed

    def fetch_caption(self, photo_id: str) -> str:
        """补取作品文案：接口不返回 ``caption``，从详情页 ``<title>`` 取。

        只在**确认有新作品**时调用（每轮至多一次），不给稳态运行加请求负担。
        取不到就返回空串 —— 文案缺失只是通知少一行字，不该让整条链路失败。
        """
        pid = str(photo_id or "").strip()
        if not pid:
            return ""
        ctx = self._ensure_ctx()
        page = None
        try:
            page = ctx.new_page()
            page.goto(photo_url(pid), wait_until="domcontentloaded",
                      timeout=self.NAV_TIMEOUT_MS)
            page.wait_for_timeout(2000)
            return clean_caption(page.title())
        except Exception as e:  # noqa: BLE001
            logger.debug("[kuaishou] 取文案失败 %s: %s", pid, e)
            return ""
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
