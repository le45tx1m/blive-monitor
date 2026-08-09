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
   必须按真实发布时间排序（时间来源见 :func:`decode_media_meta`）。

响应字段的坑
============
条目里**没有 ``timestamp``，也没有 ``caption``**，可用字段只有
``id/poster/playUrl/imgUrls/workType/counts/author`` 等。发布时间靠
:func:`decode_media_meta` 从 CDN URL 反解，文案由调用方按需补取详情页标题。
"""

import base64
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 作品接口路径（匹配用，不含 query）。页面自身请求会附带 __NS_hxfalcon 签名。
PROFILE_PUBLIC_PATH = "/live_api/profile/public"

#: 冷启动预热地址：必须先访问主站，风控 JS 才会种下 kwfv1/kwssectoken/kwscode。
#: 直接开 profile 页会因缺这些 token 恒返回 result=2。
WARMUP_URL = "https://www.kuaishou.com"

#: 作者主页（拦截目标页）
PROFILE_URL_TMPL = "https://live.kuaishou.com/profile/{pid}"

#: 作品详情页 —— 用于补文案（接口不返回 caption），同时也是通知里给用户点的链接
PHOTO_URL_TMPL = "https://www.kuaishou.com/short-video/{photo_id}"

#: 快手 CDN 路径里那段 base64 的定位模式。
#: 形如 ``/upic/2026/08/07/16/``
#: ``BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_b_B<hash>.mp4``
#: 首字符 ``B`` 是快手自己的前缀，不属于 base64 内容。
_B64_SEG_RE = re.compile(
    r"/B([A-Za-z0-9+/=-]{16,}?)(?:_b)?_[A-Za-z0-9]"
)

#: base64 解出来的载荷形如 ``20260807162057_180534002_204769936242_1_3``
#: 依次为 发布时间(yyyyMMddHHmmss) / 作者 userId / 作品数字 id / …
_PAYLOAD_RE = re.compile(r"^(\d{14})_(\d+)_(\d+)")

#: 降级：CDN 路径里的 ``/2026/08/07/16/`` 只精确到小时，好过没有
_PATH_DATE_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/(\d{2})/")

#: 图集 atlas 路径的 base64 里带毫秒时间戳：``<photoNum>_<epoch_ms>``
_ATLAS_RE = re.compile(r"/atlas/([A-Za-z0-9+/=-]{16,}?)_\d+\.")

#: 详情页标题后缀，取文案时剥掉
_TITLE_SUFFIX = ("-快手", "_快手", " - 快手")


def _b64decode_loose(s: str) -> str:
    """宽松 base64 解码（自动补 padding，兼容 URL-safe），失败返回空串。"""
    if not s:
        return ""
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    try:
        return base64.b64decode(s + "=" * pad).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _ymdhms_to_epoch(s: str) -> Optional[int]:
    """``20260807162057`` -> epoch 秒（按北京时间解释）。

    快手 CDN 路径里的时间是发布时的北京时间（+8），必须显式减 8 小时换算成
    epoch，不能用 ``datetime.timestamp()``（runner 在 UTC 下会偏 8 小时 ——
    与 :func:`kuaishou._ts_to_bj` 里踩过的是同一个坑）。
    """
    from calendar import timegm
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(s, "%Y%m%d%H%M%S") - timedelta(hours=8)
    except (TypeError, ValueError):
        return None
    return timegm(dt.timetuple())


def decode_media_meta(url: Any) -> Tuple[Optional[int], str]:
    """从快手媒体 URL 反解 ``(发布时间 epoch 秒, 作者 userId)``。

    接口不返回 ``timestamp``，但 CDN 文件名里带着它 —— 这不是猜测，是快手上传
    时用「时间_用户_作品」拼的文件名，实测 15/15 条全部解出且与作品实际发布
    时间吻合（含用户手上那条 2026-08-07 16:20:57 的最新作品）。

    顺带解出的 **userId 是天然的归属校验信号**：一页里所有作品的 userId 必须
    一致且等于该作者，可用来挡住「抓到别人作品」这类事故（本项目在抖音上踩过
    随机抓到推荐流的坑，快手这里从数据层面就能自证）。

    Returns:
        ``(epoch_seconds | None, user_id | "")``。完全解不出时返回 ``(None, "")``；
        只解出日期路径时精确到小时。
    """
    if not isinstance(url, str) or not url:
        return None, ""

    for m in _B64_SEG_RE.finditer(url):
        payload = _b64decode_loose(m.group(1))
        pm = _PAYLOAD_RE.match(payload)
        if pm:
            return _ymdhms_to_epoch(pm.group(1)), pm.group(2)

    # 图集：/ufile/atlas/<b64>_0.webp，b64 解出 "<photoNum>_<epoch_ms>"
    am = _ATLAS_RE.search(url)
    if am:
        payload = _b64decode_loose(am.group(1))
        parts = payload.split("_")
        if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) >= 13:
            return int(parts[-1]) // 1000, ""

    # 降级：路径日期只到小时
    dm = _PATH_DATE_RE.search(url)
    if dm:
        y, mo, d, h = dm.groups()
        return _ymdhms_to_epoch(f"{y}{mo}{d}{h}0000"), ""
    return None, ""


def _author_avatar(author: Any) -> str:
    """从作者对象里尽力取出头像 URL（快手字段名不统一，多候选兜底）。"""
    if not isinstance(author, dict):
        return ""
    cand = [
        author.get("avatar"),
        author.get("headUrl"),
        author.get("headurl"),
    ]
    hu = author.get("headUrls")
    if isinstance(hu, list) and hu:
        cand.append(hu[0])
    for c in cand:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def normalize_item(raw: Any) -> Optional[Dict[str, Any]]:
    """把 ``data.list`` 的一条归一化成内部 dict（时间/归属自 URL 反解）。

    Returns:
        ``{"photo_id","timestamp","user_id","cover","play_url","work_type",
        "is_image","author_id","author_name","music_name","counts"}``；
        缺 ``id`` 的脏条目返回 None（调用方跳过）。
    """
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None

    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    imgs = raw.get("imgUrls") if isinstance(raw.get("imgUrls"), list) else []
    author_avatar = _author_avatar(author)
    play_url = str(raw.get("playUrl") or "")
    cover = str(raw.get("poster") or "")

    # 时间/归属：视频优先用 playUrl（最准），图集只有 poster，最后才试图集图
    ts, uid = None, ""
    for cand in (play_url, cover, imgs[0] if imgs else ""):
        ts, uid = decode_media_meta(cand)
        if ts is not None:
            break

    work_type = str(raw.get("workType") or "")
    return {
        "photo_id": pid,
        "timestamp": ts,
        "user_id": uid,
        "cover": cover,
        "play_url": play_url,
        "work_type": work_type,
        # workType: video=视频, multiple/single=图文
        "is_image": work_type in ("multiple", "single") or (not play_url and bool(imgs)),
        "author_id": str(author.get("id") or ""),
        "author_name": str(author.get("name") or ""),
        "author_avatar": author_avatar,
        "music_name": str(raw.get("musicName") or ""),
        "counts": raw.get("counts") if isinstance(raw.get("counts"), dict) else {},
    }


def parse_profile_public(payload: Any) -> Dict[str, Any]:
    """解析 ``live_api/profile/public`` 响应。

    Returns:
        ``{"ok": bool, "result": int|None, "items": [...], "living": bool|None,
        "author_name": str, "author_id": str}``

        ``ok`` 仅在 ``result==1`` **且** 拿到条目时为 True。``result==2`` 是匿名
        被挡（预热不足），调用方应重新导航重试而非当成「没有新作品」—— 这两者
        混为一谈正是此前漏报的根因。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "result": None, "items": [], "living": None,
                "author_name": "", "author_id": ""}

    result = data.get("result")
    raw_list = data.get("list") if isinstance(data.get("list"), list) else []
    items = [x for x in (normalize_item(r) for r in raw_list) if x]

    live = data.get("live") if isinstance(data.get("live"), dict) else {}
    la = live.get("author") if isinstance(live.get("author"), dict) else {}
    living = la.get("living")
    if living is None:
        living = live.get("living")

    author_name = ""
    author_id = ""
    author_avatar = ""
    for it in items:
        author_name = author_name or it.get("author_name") or ""
        author_id = author_id or it.get("author_id") or ""
        author_avatar = author_avatar or it.get("author_avatar") or ""

    # profile 属主头像（data.user / data.author / data.owner），兜底用列表首条作品作者头像；
    # 留给前端作品卡显示，避免「有昵称没头像」的半截信息。
    if not author_avatar:
        for src in (data.get("user"), data.get("author"), data.get("owner")):
            if isinstance(src, dict):
                av = _author_avatar(src)
                if av:
                    author_avatar = av
                    break

    return {
        "ok": bool(result == 1 and items),
        "result": result,
        "items": items,
        "living": bool(living) if living is not None else None,
        "author_name": author_name,
        "author_id": author_id,
        "author_avatar": author_avatar,
    }


def sort_by_time(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按发布时间倒序（新→旧）。

    **必须显式排序**：接口返回的顺序把置顶作品放在最前，实测某账号 list[0] 是
    2025-11-05 的置顶、真正最新的 2026-08-07 排在第 4 位。直接取首条会导致
    「最新作品」永远停在那条置顶上 —— 新作永远不会被发现。

    解不出时间的条目排到最后（不参与「谁最新」的竞争，避免用未知冒充最新）。
    """
    return sorted(items, key=lambda x: (x.get("timestamp") is not None,
                                        x.get("timestamp") or 0), reverse=True)


def pick_latest(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """取真正最新的一条（按时间，非列表顺序）；无可用时间时返回 None。

    宁可返回 None 也不猜：没有可信时间就无法判断新旧，此时报「未知」比拿置顶
    作品冒充最新要诚实得多。
    """
    ordered = sort_by_time(items)
    if not ordered:
        return None
    top = ordered[0]
    return top if top.get("timestamp") is not None else None


def verify_ownership(items: List[Dict[str, Any]],
                     expect_author_id: str = "",
                     expect_user_id: str = "") -> Tuple[bool, str]:
    """校验这批作品确实属于目标账号。

    两道信号，都来自数据本身，不需要额外请求：

    * ``author.id``：条目自带的作者标识（快手的 unique_name，如 ``pineapple2005``）
    * URL 反解出的 ``userId``（如 ``180534002``）：整页必须一致

    Returns:
        ``(是否可信, 说明)``。**校验不通过必须拒绝这批数据**：宁可这轮不报，
        也不能把别人的作品当成目标账号的新作推给用户。
    """
    if not items:
        return False, "空列表"

    uids = {it["user_id"] for it in items if it.get("user_id")}
    if len(uids) > 1:
        return False, f"同页作品的 userId 不唯一（{sorted(uids)}），疑似混入他人作品"
    if expect_user_id and uids and expect_user_id not in uids:
        return False, f"userId 不匹配（期望 {expect_user_id}，实际 {sorted(uids)}）"

    aids = {it["author_id"] for it in items if it.get("author_id")}
    if len(aids) > 1:
        return False, f"同页作品的 author.id 不唯一（{sorted(aids)}）"
    if expect_author_id and aids and expect_author_id not in aids:
        return False, f"author.id 不匹配（期望 {expect_author_id}，实际 {sorted(aids)}）"
    return True, ""


def clean_caption(title: Any) -> str:
    """把详情页 ``<title>`` 洗成文案（剥掉「-快手」后缀）。"""
    s = str(title or "").strip()
    for suf in _TITLE_SUFFIX:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return "" if s in ("快手", "快手直播") else s


def photo_url(photo_id: str) -> str:
    """作品详情页链接（通知里给用户点的那个）。"""
    return PHOTO_URL_TMPL.format(photo_id=photo_id)


# ==================== 浏览器会话 ====================

class KuaishouFeedSession:
    """快手作品流的浏览器会话：**一次预热，全轮复用**。

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
    #: 每次导航后等响应的秒数
    WAIT_SEC = 7
    #: 导航超时（毫秒）
    NAV_TIMEOUT_MS = 45000

    def __init__(self, browser_context: Any, user_agent: str = "") -> None:
        self._src = browser_context
        self._ua = user_agent
        self._ctx = None
        self._warmed = False

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
        return self._ctx

    def close(self) -> None:
        """关闭自建 context（借用外部 context 时不动它）。"""
        if self._ctx is not None and self._ctx is not self._src:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = None
        self._warmed = False

    def _warmup(self, page) -> None:
        """访问主站种风控 token（``kwfv1``/``kwssectoken``/``kwscode``）。

        跳过这步的话，profile 页发出的请求会恒返回 ``result=2`` —— 这是整条
        链路唯一不可省的前置动作。
        """
        if self._warmed:
            return
        try:
            page.goto(WARMUP_URL, wait_until="domcontentloaded",
                      timeout=self.NAV_TIMEOUT_MS)
            page.wait_for_timeout(1500)
            self._warmed = True
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 主站预热失败（继续尝试）: %s", e)

    # ---- 抓取 ----
    def fetch(self, principal_id: str) -> Dict[str, Any]:  # noqa: C901
        """打开作者页、拦截页面自身的作品接口响应。

        Args:
            principal_id: 快手 principalId（如 ``3x7ju263tgi5dn9``）。

        Returns:
            :func:`parse_profile_public` 的结果；失败时 ``ok=False`` 且
            ``result`` 记录最后一次看到的状态码（2=预热不足，None=没拦到）。
        """
        import json as _json

        pid = str(principal_id or "").strip()
        if not pid:
            return {"ok": False, "result": None, "items": [], "living": None,
                    "author_name": "", "author_id": "", "detail": "缺 principalId"}

        ctx = self._ensure_ctx()
        page = ctx.new_page()
        best: Dict[str, Any] = {}
        last_result = None
        seen: List[Any] = []

        def on_response(resp):
            nonlocal best, last_result
            try:
                if PROFILE_PUBLIC_PATH not in resp.url:
                    return
                body = resp.body().decode("utf-8", "replace")
                parsed = parse_profile_public(_json.loads(body))
            except Exception:  # noqa: BLE001 —— 单条响应解析失败不影响整轮
                return
            last_result = parsed.get("result")
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
                page.wait_for_timeout(self.WAIT_SEC * 1000 // 3)
                if best:
                    break
                # result=2 是「预热还不够」，重新导航让页面重算签名/刷新 token
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

        if best:
            best["nav_count"] = len(seen)
            return best
        return {"ok": False, "result": last_result, "items": [], "living": None,
                "author_name": "", "author_id": "", "nav_count": len(seen),
                "detail": f"未拿到作品列表（响应序列={seen or '无'}）"}

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
