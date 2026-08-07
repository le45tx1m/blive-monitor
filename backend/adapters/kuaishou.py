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
from typing import Any, Dict, List, Optional

from backend.adapters.base import AdapterGated, PlatformAdapter, PostModel, RoomModel

logger = logging.getLogger(__name__)

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
    """epoch 秒 -> 北京时间字符串；失败返回空串。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


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
        try:
            # 主：visionProfilePhotoList graphql（需 did/client_key/cookie）
            # 风控/未登录返回 {"result":2} → _fetch_graphql_photos 会 raise AdapterGated
            posts = self._fetch_graphql_photos(rid)
        except AdapterGated:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 新作接口失败（降级为空）: %s", e)
            raise AdapterGated(detail="快手作品接口需 did/登录 Cookie，当前被风控")

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
            t["latest_post_id"] = last.get("photoId", "")
            t["latest_published_at"] = _ts_to_bj(_to_ts(last.get("timestamp")))
        return out

    def _fetch_graphql_photos(self, rid: str) -> List[Dict[str, Any]]:
        """调用 visionProfilePhotoList（需 did/client_key/cookie）。

        风控/未登录：响应 ``{"result":2}`` → raise AdapterGated（记为 gated，而非「无新作」）。
        """
        url = "https://www.kuaishou.com/graphql"
        body = json.dumps({
            "operationName": "visionProfilePhotoList",
            "variables": {"userId": rid, "page": 1},
            "query": (
                "query visionProfilePhotoList($userId:String,$page:Int){"
                "visionProfilePhotoList(userId:$userId,page:$page){photoId caption "
                "coverUrl url timestamp is_image isVideo}}"
            ),
        }).encode("utf-8")
        hdr = {"Content-Type": "application/json",
               "Referer": "https://www.kuaishou.com/"}
        if self.did:
            hdr["Cookie"] = f"did={self.did}; client_key={self.client_key}"
        elif self.cookie:
            hdr["Cookie"] = self.cookie
        req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        # 风控/未登录：result 为非 0 / 非 None（实测 {"result":2,"error_msg":null}）
        if isinstance(d, dict) and d.get("result") not in (None, 0, "0"):
            raise AdapterGated(detail=f"快手作品接口被风控(result={d.get('result')})，需 did/登录 Cookie")
        feeds = ((d.get("data") or {}).get("visionProfilePhotoList") or {}).get("feeds") or []
        return feeds


def _as_int(v: Any) -> int:
    """把可能是字符串/数字的值安全转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_initial_state(html: Any) -> Optional[Dict[str, Any]]:
    """从 HTML 稳健提取 window.__INITIAL_STATE__ 的对象（括号配平，兼容嵌套）。

    SSR 页中该对象体积大、含嵌套结构，正则非贪婪易截断，故用栈配平大括号提取。
    失败返回 None（调用方降级为 offline）。
    """
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", "replace")
    marker = "window.__INITIAL_STATE__"
    idx = html.find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    i = start
    n = len(html)
    end = -1
    while i < n:
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    if end < 0:
        return None
    try:
        return json.loads(html[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
