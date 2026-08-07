"""KuaishouAdapter：快手直播 + 新作（阶段三 T02）。

直播：主路径 **SSR 解析** ``live.kuaishou.com/u/<id>`` 的 ``window.__INITIAL_STATE__``。
      真实结构（已实测）：直播态在 ``liveroom.playList[0].isLiving``，详情在
      ``liveroom.playList[0].liveStream.{caption,coverUrl,watcherCount}``；
      旧结构（``liveroom.living/caption``）作为兼容回退保留。
      不再调用已废弃的 ``liveroomDetail`` 接口（实测 HTTP 404）。

新作：``visionProfilePhotoList`` graphql（带 did/client_key/cookie）。
      识别 ``result:2``（风控/未登录）并 **raise AdapterGated**，记为 gated 而非「无新作」。

匿名 scraping 尽力而为：数据中心 IP / 缺 Cookie 易触发风控（返回空/离线），
失败一律优雅降级为 offline / 无新作（绝不抛未捕获异常中断整轮）。

⚠️ 提升命中率：在 BLIVE_CONFIG.platforms.kuaishou.credentials 配置
``did``（匿名可随机生成）/``cookie``（登录态）可突破部分风控。
"""

import json
import logging
import re
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from backend.adapters.base import AdapterGated, PlatformAdapter, PostModel, RoomModel
from common import bjnow, epoch_to_beijing
from backend.adapters.identity import (
    CredentialLadder,
    CredentialLevel,
    IdentityCache,
    PrincipalIdentity,
)
from backend.adapters.kuaishou_identity import (
    KuaishouIdentityResolver,
    looks_like_principal_id as _looks_like_principal_id,
)

logger = logging.getLogger(__name__)

#: 进程内共享的身份缓存 —— 同一轮监控里多个账号/多次调用不重复解析。
#: 跨轮持久化交给 tracking 的 ``principal_id`` 字段（见 apply_identity_to_tracking）。
_identity_cache = IdentityCache()

#: 从 config 条目里认得的身份提示字段（用户填了就用，没填 resolver 自己找）
_HINT_KEYS = ("principal_id", "nickname", "unique_name", "share_user_id",
              "room_id", "live_id", "home_url", "share_url", "seed_url", "photo_id")


def build_identity_hints(entry: Any, tracking: Any = None) -> Dict[str, Any]:
    """把 config 条目 + 已有 tracking 合成 resolver 的 hints。

    tracking 里的 ``principal_id`` 是上一轮解析并校验过的结果，直接复用可以让稳态
    运行时的身份解析降到零次网络请求 —— 这也是跨轮次的持久化缓存。
    config 优先级高于 tracking（用户改配置能立刻生效）。
    """
    hints: Dict[str, Any] = {}
    if isinstance(tracking, dict):
        for key in ("principal_id", "nickname", "unique_name"):
            if tracking.get(key):
                hints[key] = str(tracking[key])
    if isinstance(entry, dict):
        for key in _HINT_KEYS:
            if entry.get(key):
                hints[key] = str(entry[key])
        # config 里的 name 当昵称用（不覆盖更明确的 nickname 字段）
        if entry.get("name") and not hints.get("nickname"):
            hints["nickname"] = str(entry["name"])
    return hints


#: 已校验身份的信任期：这段时间内不再重复交叉校验（秒）。
#:
#: 为什么需要它：CI 每 5 分钟一轮，而一次完整校验要打 2 次 live.kuaishou.com
#: （输入侧 + principalId 侧）。每轮都验 = 每账号每天约 864 次请求，
#: 实测十几次连打就会进入 **11 分钟以上** 的 IP 级限流惩罚期 —— 那样反而
#: 什么都监控不到。principalId 是账号级稳定标识（originUserId 更是终生不变），
#: 「首次严格校验 + 24 小时内信任 + 到期重验」在准确性上的损失可以忽略，
#: 请求量却降到 1/288。这不是放宽标准，是把校验预算花在刀刃上。
IDENTITY_TRUST_SEC = 24 * 3600


def identity_from_tracking(tracking: Any, rid: str = "") -> Optional[PrincipalIdentity]:
    """从 tracking 恢复上一轮**已校验且仍在信任期内**的身份。

    这是跨进程的身份缓存：CI 每轮都是全新进程，内存缓存必然落空，
    但 tracking 会随仓库提交回来，天然就是持久层。

    只有「验过的」才享受信任期；没验过的（identity_verified=False）
    必须走完整解析流程，否则等于把未经证实的猜测长期固化。
    """
    if not isinstance(tracking, dict):
        return None
    pid = str(tracking.get("principal_id") or "")
    if not pid or not tracking.get("identity_verified"):
        return None
    refreshed = _parse_bj(tracking.get("last_identity_refresh"))
    if refreshed is None:
        return None
    age = (bjnow() - refreshed).total_seconds()
    if age < 0 or age > IDENTITY_TRUST_SEC:
        return None
    ident = PrincipalIdentity(
        platform="kuaishou",
        principal_id=pid,
        nickname=str(tracking.get("nickname") or ""),
        unique_name=str(tracking.get("unique_name") or ""),
        identity_source=str(tracking.get("identity_source") or "tracking"),
        confidence=1.0,
        last_updated=str(tracking.get("last_identity_refresh") or ""),
    )
    ident.trace = ["tracking_trusted"]
    ident.extra["verified"] = True
    ident.extra["trusted_from_tracking"] = True
    origin = tracking.get("origin_user_id")
    if origin:
        ident.extra["origin_user_id"] = str(origin)
    return ident


def _parse_bj(s: Any) -> Optional[datetime]:
    """解析 ``YYYY-MM-DD HH:MM:SS`` 北京时间；失败返回 None。"""
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def resolve_kuaishou_identity(entry: Any, rid: str, tracking: Any = None,
                              resolver: Optional[KuaishouIdentityResolver] = None,
                              ) -> Optional[PrincipalIdentity]:
    """解析快手账号身份（Fail Soft，解不出返回 None）。

    Identity Framework 的统一入口：不再手写「有 principal_id 就用、否则扒 seed_url」
    那套散装逻辑，交给 resolver 的策略流水线 + originUserId 交叉校验。

    稳态快路径：上一轮已校验且在信任期内 → 直接复用，零请求（见
    :data:`IDENTITY_TRUST_SEC`）。但用户在 config 里改了 principal_id 时立即失效，
    否则「改了配置不生效」会让人抓狂。
    """
    trusted = identity_from_tracking(tracking, rid)
    if trusted is not None:
        cfg_pid = str((entry or {}).get("principal_id") or "") if isinstance(entry, dict) else ""
        if not cfg_pid or cfg_pid == trusted.principal_id:
            return trusted
        logger.info("[kuaishou] config 的 principal_id 已变更（%s → %s），放弃信任期重新解析",
                    trusted.principal_id, cfg_pid)
    r = resolver or KuaishouIdentityResolver(cache=_identity_cache)
    return r.resolve(rid, hints=build_identity_hints(entry, tracking))


def apply_identity_to_tracking(ident: Optional[PrincipalIdentity],
                               tracking: Dict[str, Any]) -> None:
    """把解析到的身份写回 tracking，供下一轮零成本复用。"""
    if ident is None or not isinstance(tracking, dict):
        return
    tracking["principal_id"] = ident.principal_id
    tracking["identity_source"] = ident.identity_source
    tracking["last_identity_refresh"] = ident.last_updated
    tracking["identity_verified"] = bool((ident.extra or {}).get("verified"))
    if ident.nickname and not tracking.get("nickname"):
        tracking["nickname"] = ident.nickname
    if ident.unique_name:
        tracking["unique_name"] = ident.unique_name
    origin = (ident.extra or {}).get("origin_user_id")
    if origin:
        tracking["origin_user_id"] = str(origin)


def apply_identity_to_config(ident: Optional[PrincipalIdentity],
                             entry: Dict[str, Any]) -> bool:
    """把解析到的身份补进 config 条目（任务八：用户不填则自动补齐）。

    **只填空位**：用户手填的值永远优先，解析结果只补用户没说的部分。
    发现两者冲突时不静默覆盖，而是明确告警 —— 冲突要么是用户配错了人，
    要么是我们解错了人，两种都必须被看见，绝不能悄悄和稀泥。

    Returns:
        是否写入了新字段（供调用方决定要不要落盘）。
    """
    if ident is None or not isinstance(entry, dict):
        return False
    extra = ident.extra or {}
    candidates = {
        "principal_id": ident.principal_id,
        "origin_user_id": str(extra.get("origin_user_id") or ""),
        "nickname": ident.nickname,
        "unique_name": ident.unique_name,
        "home_url": ident.home_url,
        "share_url": ident.share_url,
        "room_id": ident.room_id,
        "identity_source": ident.identity_source,
    }
    verified = bool(extra.get("verified"))
    changed = False
    for field, val in candidates.items():
        if not val:
            continue
        cur = entry.get(field)
        if not cur:
            entry[field] = val
            changed = True
        elif str(cur) != str(val) and field in ("principal_id", "origin_user_id"):
            # 主键级冲突：以用户配置为准，但必须留下痕迹
            logger.warning(
                "[kuaishou] config 里的 %s=%s 与解析结果 %s 不一致（校验=%s）—— "
                "以 config 为准；若通知里的人不对，请核对该字段",
                field, cur, val, "已通过" if verified else "未做",
            )
    return changed


def resolve_kuaishou_principal_id(entry: Any, rid: str,
                                  http_get: Optional[Callable[[str], bytes]] = None,
                                  tracking: Any = None) -> Optional[str]:
    """解析 principalId（兼容旧签名的薄封装，内部走 Identity Framework）。

    ``http_get`` 仅为不破坏既有调用方而保留：resolver 需要拿到重定向**终链**
    （分享链接的 userId 就藏在那），旧的 ``bytes`` 返回值表达不了这个信息。
    """
    ident = resolve_kuaishou_identity(entry, rid, tracking=tracking)
    return ident.principal_id if ident else None

_KUAISHOU_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_DEFAULT_CLIENT_KEY = "3c7cd4d734b53483"


def _to_ts(v: Any) -> Optional[int]:
    """尽力把时间值转成 epoch 秒（兼容 int / 字符串）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts_to_bj(ts: Optional[int]) -> str:
    """epoch 秒 -> 北京时间字符串；失败返回空串。

    必须显式指定 +8 时区，不能用裸 ``datetime.fromtimestamp()``：
    GitHub Actions runner 默认 UTC 且 workflow 未设 ``TZ``，
    裸调用会把 UTC 时间当成北京时间写进 tracking，整体偏早 8 小时。
    """
    return epoch_to_beijing(ts)


def _now_bj() -> str:
    """当前北京时间字符串（显式 +8，理由同 :func:`_ts_to_bj`）。"""
    return bjnow().strftime("%Y-%m-%d %H:%M:%S")


class KuaishouAdapter(PlatformAdapter):
    platform = "kuaishou"
    supports_live = True
    supports_posts = True
    poll_interval = 300
    rate_limit = {"max_requests": 20, "window_sec": 60, "backoff_sec": 30}
    needs_context = False  # SSR 主路径无需浏览器；Playwright 降级留作 P2 增强

    def __init__(self, credentials: Optional[Dict[str, Any]] = None,
                 poll_interval: Optional[int] = None,
                 rate_limit: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(credentials or {}, poll_interval, rate_limit)
        self.did = str(self.credentials.get("did") or "")
        self.client_key = str(self.credentials.get("client_key") or _DEFAULT_CLIENT_KEY)
        self.cookie = str(self.credentials.get("cookie") or "")
        #: 最近一次 graphql 的凭证阶梯结果（编排层据此写 tracking）
        self.last_ladder = None
        #: 本适配器实例累计命中风控的次数
        self.gated_count = 0

    # ---- 网络（可被测试 monkeypatch）----
    def _http_get(self, url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 10) -> bytes:
        hdr = {"User-Agent": _KUAISHOU_UA, "Referer": "https://live.kuaishou.com/"}
        if self.cookie:
            hdr["Cookie"] = self.cookie
        elif self.did:
            hdr["Cookie"] = f"did={self.did}"
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    # ---------- 直播 ----------
    def fetch_room_status(self, room_id: str) -> RoomModel:
        """取快手直播间状态（SSR 解析为主，失败优雅降级 offline）。"""
        room_id = str(room_id)
        try:
            html = self._http_get(
                f"https://live.kuaishou.com/u/{room_id}", timeout=10
            ).decode("utf-8", "replace")
            return self._room_from_html(room_id, html)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[kuaishou] 直播 SSR 解析失败（降级 offline）: %s", e
            )
            return RoomModel(
                platform="kuaishou", room_id=room_id, live_status=False,
                extra={"degraded": True},
            )

    @staticmethod
    def _room_from_html(room_id: str, html: str) -> RoomModel:
        """SSR 解析 window.__INITIAL_STATE__（纯函数，便于单测）。

        兼容两种结构：
          - 真实结构：``liveroom.playList[0].isLiving`` + ``.liveStream.*``
          - 旧/兼容结构：``liveroom.living/caption/watcherCount/coverUrl``
        """
        state = _extract_initial_state(html)
        if not state:
            return RoomModel(platform="kuaishou", room_id=room_id, live_status=False)

        liveroom = state.get("liveroom") or {}
        play_list = liveroom.get("playList") or []

        living = False
        title = ""
        online = 0
        cover = ""

        if play_list:
            # 真实快手 SSR 结构（实测确认）
            pl0 = play_list[0] or {}
            living = bool(pl0.get("isLiving"))
            ls = pl0.get("liveStream") or {}
            title = ls.get("caption") or pl0.get("caption") or ""
            online = _as_int(
                ls.get("watcherCount") or ls.get("viewerCount")
                or pl0.get("watcherCount") or liveroom.get("watcherCount")
            )
            cover = (
                ls.get("coverUrl") or ls.get("poster")
                or pl0.get("coverUrl") or liveroom.get("coverUrl") or ""
            )
        else:
            # 兼容旧结构
            living = bool(liveroom.get("living"))
            if living:
                title = liveroom.get("caption") or ""
                online = _as_int(liveroom.get("watcherCount"))
                cover = liveroom.get("coverUrl") or ""

        return RoomModel(
            platform="kuaishou",
            room_id=room_id,
            title=title,
            live_status=living,
            online=online,
            cover=cover,
            extra={"living": living, "source": "ssr"},
        )

    # ---------- 新作 ----------
    def fetch_new_posts(self, author_or_room: str, since: Optional[datetime] = None,
                        baseline: Optional[Dict[str, Any]] = None,
                        context: Any = None) -> List[PostModel]:
        rid = str(author_or_room)
        t = baseline if isinstance(baseline, dict) else {}
        if not _looks_like_principal_id(rid):
            # 传进来的不是 principalId（多半是用户名）——graphql 会安静地返回
            # feeds=[]，看起来像「没有新作品」，实则是身份没解对。这里显式点破，
            # 否则这类问题会一直伪装成「这个号最近没更新」。
            logger.warning(
                "[kuaishou] %s 不是 principalId 形态，graphql 大概率返回空。"
                "请在 post_rooms.json 补 principal_id / share_url / seed_url", rid,
            )
        try:
            # 主：visionProfilePhotoList graphql，凭证三级降级（匿名→did→Cookie）
            posts = self._fetch_graphql_photos(rid)
        except AdapterGated:
            self._write_run_tracking(t, rid, success=False)
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 新作接口失败（降级为空）: %s", e)
            self.gated_count += 1
            self._write_run_tracking(t, rid, success=False)
            raise AdapterGated(detail=f"快手作品接口请求失败：{e}")

        out: List[PostModel] = []
        prev_id = t.get("latest_post_id", "")
        prev_ts = _to_ts(t.get("latest_published_at"))
        for p in posts:
            pid = p.get("photoId", "")
            ts = _to_ts(p.get("timestamp"))
            # 仅返回「比基线新」的作品（id 不同且时间更新；无时间则仅按 id 去重）
            if pid and pid != prev_id and (prev_ts is None or ts is None or ts > prev_ts):
                is_image = bool(p.get("is_image", False)) or not bool(p.get("isVideo", True))
                out.append(PostModel(
                    platform="kuaishou",
                    post_id=pid,
                    author=t.get("nickname", "") or p.get("author", "") or p.get("userName", ""),
                    # 直接用可靠提取到的 photoId 构造规范视频页链接（fw/photo/{photoId}），
                    # 不再盲信 API 可能返回的「fw/photo/{用户名}」错误 url（会报「参数格式错误」）。
                    url=(f"https://v.m.chenzhongtech.com/fw/photo/{pid}" if pid
                         else (p.get("url") or p.get("webUrl") or p.get("photoUrl") or "")),
                    cover=p.get("coverUrl") or p.get("cover") or "",
                    published_at=_ts_to_bj(ts),
                    title=p.get("caption", ""),
                    extra={
                        "conf": "api",
                        "type": "图文" if is_image else "视频",
                        "dedup_key": f"post:kuaishou:{pid}",
                    },
                ))
        # 更新基线（取最新一条）
        if posts:
            last = max(posts, key=lambda x: _to_ts(x.get("timestamp")) or 0)
            latest_ts = _to_ts(last.get("timestamp"))
            t["latest_post_id"] = last.get("photoId", "")
            t["latest_published_at"] = _ts_to_bj(latest_ts)
            if latest_ts is not None:
                t["latest_timestamp"] = latest_ts
        self._write_run_tracking(t, rid, success=True)
        return out

    def _write_run_tracking(self, t: Dict[str, Any], rid: str, success: bool) -> None:
        """写入本轮运行观测字段（任务七）。

        这些字段不参与去重判断，纯粹是为了让「为什么没抓到」可回答：
        是身份没解对、还是被风控、还是要更高等级的凭证。
        """
        if not isinstance(t, dict):
            return
        # 入参即 principalId 时固化下来，避免下一轮重复解析
        if _looks_like_principal_id(rid):
            t.setdefault("principal_id", rid)
        if success:
            t["last_success"] = _now_bj()
        ladder = self.last_ladder
        if ladder is not None:
            t["cookie_used"] = ladder.level_used == CredentialLevel.COOKIE
            t["did_used"] = ladder.level_used == CredentialLevel.DEVICE
            t["credential_level"] = ladder.level_used or ""
        if self.gated_count:
            t["gated_count"] = self.gated_count

    # ---------- graphql（三级凭证降级）----------
    @staticmethod
    def _is_gated(payload: Any) -> bool:
        """判定响应是否被风控。

        快手不用 HTTP 状态码表达风控，而是 200 + ``result`` 非 0：
        ``result=2`` 匿名被拦、``result=400002`` 验证码挑战。
        """
        return isinstance(payload, dict) and payload.get("result") not in (None, 0, "0")

    def _graphql_once(self, rid: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """发一次 visionProfilePhotoList 请求（单级凭证，由阶梯调用）。"""
        body = json.dumps({
            "operationName": "visionProfilePhotoList",
            "variables": {"userId": rid, "page": 1},
            "query": (
                "query visionProfilePhotoList($userId:String,$page:Int){"
                "visionProfilePhotoList(userId:$userId,page:$page){photoId caption "
                "coverUrl url timestamp is_image isVideo}}"
            ),
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://www.kuaishou.com/graphql", data=body,
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _fetch_graphql_photos(self, rid: str) -> List[Dict[str, Any]]:
        """取作品列表，凭证按 L1 匿名 → L2 did → L3 Cookie 逐级升级。

        原则是**能用弱凭证就别掏登录态**：匿名能过就不带 did，did 能过就不带 Cookie，
        既降低账号暴露风险，也让「哪一级才够用」变成可观测数据（写入 tracking 的
        ``cookie_used`` / ``did_used``）。全部等级都被风控才 raise AdapterGated —— 这
        与「没有新作」是两回事，编排层据此记 cookie_warn 而不是静默。
        """
        ladder = CredentialLadder(
            did=self.did, cookie=self.cookie, client_key=self.client_key,
            extra_headers={
                "Content-Type": "application/json",
                "User-Agent": _KUAISHOU_UA,
                "Referer": "https://www.kuaishou.com/",
            },
        )
        result = ladder.run(
            lambda headers, level: self._graphql_once(rid, headers),
            should_retry=self._is_gated,
        )
        self.last_ladder = result  # 供编排层写 tracking（哪一级成功/试过几级）

        if not result.ok:
            tried = "/".join(result.levels_tried) or "无可用凭证"
            self.gated_count += 1
            raise AdapterGated(
                detail=f"快手作品接口被风控（已试 {tried}），"
                       f"可在 BLIVE_CONFIG.platforms.kuaishou.credentials 配置 cookie 突破"
            )

        data = result.value if isinstance(result.value, dict) else {}
        return ((data.get("data") or {}).get("visionProfilePhotoList") or {}).get("feeds") or []


#: 快手 SSR 里会出现的 JS 专有字面量（非合法 JSON），解析失败时替换为 null 重试。
#: 前后用 (?<!["\w]) / (?!["\w]) 守卫，避免误伤字符串内容里的同名单词。
_JS_LITERAL_RE = re.compile(
    r'(?<![\w"])(?:undefined|NaN|-?Infinity|void 0)(?![\w"])'
)


def _as_int(v: Any) -> int:
    """把可能是字符串/数字的值安全转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_initial_state(html: Any) -> Optional[Dict[str, Any]]:
    """从 HTML 稳健提取 window.__INITIAL_STATE__ 的对象（括号配平，兼容嵌套）。

    两个必须处理的坑（2026-08 实测踩到）：

    1. **括号可能出现在字符串字面量里**（标题/描述常含 ``{``），朴素配平会提前收尾，
       所以扫描时要跳过引号内内容并正确处理反斜杠转义。
    2. **快手 SSR 是 JS 对象字面量，不是严格 JSON**：实测页面里有
       ``"authToken":undefined``，``json.loads`` 直接抛 JSONDecodeError。
       此前该异常被吞掉返回 None，导致 ``fetch_room_status`` 把**正在直播的房间
       静默判成 offline**（开播提醒漏报的真凶）。这里对 ``undefined`` / ``NaN`` /
       ``Infinity`` / ``void 0`` 做一次保守替换后重试。

    失败返回 None（调用方降级为 offline）。
    """
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", "replace")
    if not isinstance(html, str):
        return None
    marker = "window.__INITIAL_STATE__"
    idx = html.find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    end = -1
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    raw = html[start:end + 1]
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 —— 大概率是 JS 字面量，降级重试
        pass
    try:
        return json.loads(_JS_LITERAL_RE.sub("null", raw))
    except Exception as e:  # noqa: BLE001
        logger.debug("[kuaishou] SSR 状态解析失败: %s", e)
        return None
