"""快手作品流抓取 · 纯逻辑核心（无浏览器依赖，可单测）。

本模块只放**不依赖 Playwright / 网络**的纯函数与常量：URL 形态、响应解析、
时间戳反解、归属校验、文案清洗。浏览器会话（``KuaishouFeedSession``）放在同包的
``kuaishou_feed.py``，那里才 import 本模块并负责「预热种 token → 拦截页面自身请求」。

把纯逻辑单独成模块的目的：
* 单测无需启动 Chromium，CI 快、稳、可离线跑；
* 浏览器会话的改动不会牵连解析逻辑；
* 解析逻辑是「取 list[0] 即错 / 必须按 URL 反解时间 / 必须校验归属」这类踩坑重灾区，
  值得用断言死死锁住。

接口背景见 ``kuaishou_feed.py`` 模块文档：``live_api/profile/public`` 是免登录通道，
``result=2`` 是匿名被挡（需浏览器预热），``list`` 不按时间倒序、条目无 timestamp/caption。
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

#: 主站风控 JS 会种下的关键 token；预热后必须存在，否则 profile 接口恒返回
#: ``result=2``（匿名被挡/预热不足）。实测 ``domcontentloaded`` 时机偏早、常常只种下
#: ``kwscode``/``kwssectoken``，需等 ``networkidle`` 让风控 JS 跑完才会补 ``kwfv1``。
ANTIBOT_COOKIES = ("kwfv1", "kwssectoken", "kwscode")

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
        "author_name": str, "author_id": str, "author_avatar": str}``

        ``ok`` 仅在 ``result==1`` **且** 拿到条目时为 True。``result==2`` 是匿名
        被挡（预热不足），调用方应重新导航重试而非当成「没有新作品」—— 这两者
        混为一谈正是此前漏报的根因。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "result": None, "items": [], "living": None,
                "author_name": "", "author_id": "", "author_avatar": ""}

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
