"""通用 Identity Resolution Framework（跨平台账号身份归一化）。

问题背景：各平台「用户可见的标识」与「接口真正需要的标识」往往不是同一个东西。
以快手为例，用户手里拿到的是用户名 ``Sandy88888`` / 分享链接 / 直播间链接，
但 ``visionProfilePhotoList`` graphql 真正需要的 ``userId`` 是 principalId
（形如 ``3xrgxqkqp829xz6``）；传错则接口返回 ``feeds=[]``，看起来像「没有新作品」，
实则是身份没解析对。抖音的 sec_uid、小红书的 user_id 同理。

本模块把这件事抽象成平台无关的框架：

- :class:`PrincipalIdentity` —— 统一身份模型（归一化后的账号事实）。
- :class:`IdentityQuery` —— 归一化后的解析输入（原始串 + 类型 + 提示）。
- :class:`ResolveStrategy` —— 单条解析策略（一种证据来源），按优先级排队。
- :class:`IdentityResolver` —— 模板方法：分类 → 查缓存 → 逐策略解析 → 合并 →
  校验 → 落缓存。**``resolve()`` 永不抛异常（Fail Soft）**，解析不出返回 ``None``。
- :class:`IdentityCache` —— 带 TTL 的 JSON 文件缓存，IO 异常自动退化为纯内存。
- :class:`CredentialLadder` —— 凭证三级降级（匿名 → 设备 did → 登录 Cookie），
  记录最终成功用的是哪一级，供 tracking 观测风控强度。

硬约束（与 adapters/base.py 一致）：本模块只做「解析 + 归一化」，绝不直写业务
JSON/DB，也不做通知；缓存文件是解析器自有的旁路缓存，不属于业务状态。

设计红线：
- **禁止**为让某个账号跑通而硬编码 principal_id；
- **禁止**手工维护「昵称 → id」映射表；
- **禁止**任何账号级 if 特判。
账号专属信息只能来自 config 显式配置（用户填的）或线上证据（HTTP 实证）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 身份缓存默认有效期（秒）。principalId 是账号级稳定标识，几乎不变，
#: 但仍设 TTL 以便账号注销/改名后能自愈。
DEFAULT_IDENTITY_TTL = 7 * 24 * 3600

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class IdentityKind:
    """解析输入的类型（由各平台 resolver 的 ``classify()`` 判定）。"""

    PRINCIPAL_ID = "principal_id"
    UNIQUE_NAME = "unique_name"
    NICKNAME = "nickname"
    SHARE_URL = "share_url"
    HOME_URL = "home_url"
    LIVE_URL = "live_url"
    POST_URL = "post_url"
    ROOM_ID = "room_id"
    UNKNOWN = "unknown"


class VerifyOutcome:
    """校验三态 —— 关键在于把「校验没做成」和「校验不通过」分开。

    早期实现只有 bool，网络抖动/风控导致校验请求失败时只能返回 True 放行，
    等于用 Fail Soft 悄悄降低了判断标准（实测踩到：突发 501 让「配错人」的
    负例蒙混过关）。三态把这件事显式化：

    * ``PASS``    —— 证据比对一致，可信，正常缓存。
    * ``UNKNOWN`` —— 校验请求本身失败，无法判断。放行但标记未校验 + **短 TTL**，
      下一轮自动重试，不会永久固化一个没验过的身份。
    * ``FAIL``    —— 证据明确不一致（解错人），丢弃。
    """

    PASS = "pass"
    UNKNOWN = "unknown"
    FAIL = "fail"


class IdentitySource:
    """身份来源标记（写入 ``identity_source``，供 tracking 观测证据质量）。"""

    CONFIG = "config"          # 用户在 config 显式配置，最可信
    CACHE = "cache"            # 命中本地缓存
    INPUT = "input"            # 输入本身就是目标 id（形态匹配）
    LIVE_URL = "live_url"      # 直播间链接路径
    HOME_URL = "home_url"      # 主页链接路径
    SHARE_REDIRECT = "share_redirect"  # 分享短链 302 终链参数
    PAGE_SSR = "page_ssr"      # 页面 SSR 内联 JSON
    API = "api"                # 平台接口
    SEARCH = "search"          # 搜索接口
    UNKNOWN = "unknown"


def _now_bj() -> str:
    """当前北京时间字符串（与项目其它模块的时间格式保持一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# HTTP：框架自带一个 Fail Soft 的最小客户端，平台 resolver 可注入替换
# --------------------------------------------------------------------------
@dataclass
class HttpResponse:
    """归一化 HTTP 响应。

    关键点是 ``url``：它是**跟随重定向后的终链**，分享短链的 principalId
    正是藏在终链 query（``?userId=3x...``）里，因此终链本身就是一类证据。
    失败时 ``status=0``、``ok=False``，绝不抛异常。
    """

    url: str = ""
    status: int = 0
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def http_fetch(url: str, headers: Optional[Dict[str, str]] = None,
               timeout: int = 10) -> HttpResponse:
    """GET 一个 URL 并返回 :class:`HttpResponse`（Fail Soft，永不抛异常）。"""
    hdr = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdr.update({k: v for k, v in headers.items() if v})
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return HttpResponse(
                url=r.geturl(),
                status=getattr(r, "status", 200) or 200,
                text=body.decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as e:  # 4xx/5xx 仍是有效证据（如 404=账号不存在）
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return HttpResponse(url=url, status=int(e.code or 0), text=text,
                            error=str(e))
    except Exception as e:  # noqa: BLE001
        return HttpResponse(url=url, status=0, text="", error=str(e))


#: 注入式 HTTP 客户端签名：``(url, headers, timeout) -> HttpResponse``
HttpFetcher = Callable[..., HttpResponse]


# --------------------------------------------------------------------------
# 统一身份模型
# --------------------------------------------------------------------------
@dataclass
class PrincipalIdentity:
    """统一身份模型：一个账号在某平台上的全部已知标识。

    字段分三类：
      * **主键**：``principal_id`` —— 平台接口真正需要的账号 id（快手 principalId、
        抖音 sec_uid、小红书 user_id）。这是整个框架要解出来的东西。
      * **别名**：``nickname`` / ``unique_name`` / ``share_user_id`` / ``room_id`` /
        ``live_id`` —— 用户能看到、能输入的各种标识，用于反查与展示。
      * **溯源**：``identity_source`` / ``confidence`` / ``resolved_at`` /
        ``last_updated`` / ``trace`` —— 记录「这个 id 是怎么来的」，便于事后审计。

    ``extra`` 承载平台专属字段（如快手的 ``origin_user_id``），不污染通用模型。
    """

    platform: str = ""
    principal_id: str = ""
    nickname: str = ""
    unique_name: str = ""
    share_user_id: str = ""
    room_id: str = ""
    live_id: str = ""
    home_url: str = ""
    share_url: str = ""
    identity_source: str = IdentitySource.UNKNOWN
    confidence: float = 0.0
    resolved_at: float = 0.0
    last_updated: str = ""
    #: 本条独有的缓存有效期（秒）；0 表示用缓存的全局 TTL。
    #: 未通过校验的身份用它来实现「短命缓存、下轮重验」。
    ttl: float = 0.0
    trace: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---- 状态判定 ----
    def is_resolved(self) -> bool:
        """是否已解出主键（能拿去调接口）。"""
        return bool(self.principal_id)

    def age_seconds(self, now: Optional[float] = None) -> float:
        if not self.resolved_at:
            return float("inf")
        return max(0.0, (now if now is not None else time.time()) - self.resolved_at)

    def is_expired(self, ttl: float = DEFAULT_IDENTITY_TTL,
                   now: Optional[float] = None) -> bool:
        # 条目自带 ttl 优先（未校验身份会带一个很短的 ttl 以便下轮重验）
        effective = float(self.ttl) if self.ttl else float(ttl)
        return self.age_seconds(now) > effective

    def touch(self, now: Optional[float] = None) -> "PrincipalIdentity":
        """刷新解析时间戳。"""
        self.resolved_at = now if now is not None else time.time()
        self.last_updated = _now_bj()
        return self

    # ---- 合并 ----
    def merge(self, other: Optional["PrincipalIdentity"]) -> "PrincipalIdentity":
        """把 ``other`` 的信息并入自身：**只填空位，已有值不被覆盖**。

        这是「先验优先」原则：越早跑的策略优先级越高（config > 输入形态 >
        线上证据），后面的策略只能补充它没说过的字段，不能推翻前面的结论。
        ``trace`` 累加，``confidence`` 取较大者。
        """
        if other is None:
            return self
        for key in ("platform", "principal_id", "nickname", "unique_name",
                    "share_user_id", "room_id", "live_id", "home_url", "share_url"):
            if not getattr(self, key, "") and getattr(other, key, ""):
                setattr(self, key, getattr(other, key))
        if self.identity_source in ("", IdentitySource.UNKNOWN) and other.identity_source:
            self.identity_source = other.identity_source
        self.confidence = max(float(self.confidence or 0), float(other.confidence or 0))
        for t in other.trace or []:
            if t not in self.trace:
                self.trace.append(t)
        for k, v in (other.extra or {}).items():
            self.extra.setdefault(k, v)
        return self

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["PrincipalIdentity"]:
        """从 dict 还原（未知字段忽略，脏数据返回 None —— Fail Soft）。"""
        if not isinstance(d, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kw = {k: v for k, v in d.items() if k in known}
        if not kw:  # 一个已知字段都没有 = 脏数据，不还原出空壳污染缓存
            return None
        try:
            ident = cls(**kw)
        except Exception as e:  # noqa: BLE001
            logger.debug("[identity] 反序列化失败，忽略该条: %s", e)
            return None
        if not isinstance(ident.trace, list):
            ident.trace = []
        if not isinstance(ident.extra, dict):
            ident.extra = {}
        return ident


# --------------------------------------------------------------------------
# 解析输入 / 解析策略
# --------------------------------------------------------------------------
@dataclass
class IdentityQuery:
    """归一化后的解析输入。

    ``raw`` 是用户/config 给的原始串（用户名、昵称、房间号、任意一种链接）；
    ``kind`` 由平台 resolver 的 ``classify()`` 判定；``hints`` 是 config 里
    已知的其它字段（nickname/room_id/share_url/...），可让策略少跑几次网络。
    """

    raw: str = ""
    platform: str = ""
    kind: str = IdentityKind.UNKNOWN
    hints: Dict[str, Any] = field(default_factory=dict)

    def hint(self, key: str, default: str = "") -> str:
        v = (self.hints or {}).get(key)
        return str(v) if v not in (None, "") else default

    @property
    def cache_key(self) -> str:
        """缓存键：优先用最稳定的标识，避免同一账号在缓存里裂成多条。"""
        stable = (self.hint("principal_id") or self.raw or self.hint("unique_name")
                  or self.hint("nickname"))
        return f"{self.platform}:{stable}".strip().lower()


class ResolveStrategy(ABC):
    """单条解析策略 = 一种证据来源。

    每条策略只回答「我这条线索能挖出什么」，返回**部分** :class:`PrincipalIdentity`
    （只填自己有把握的字段），由 resolver 负责合并。挖不到返回 ``None``。

    约定：
      * ``run()`` 内部应自行处理异常并返回 ``None``；即便漏抛，resolver 也会兜住。
      * ``applies()`` 用来省掉无谓的网络请求（如输入不是链接就别跑链接策略）。
      * ``cost`` 表示代价（0=纯本地，1=一次请求，2=多次请求），resolver 按
        ``cost`` 升序执行，先本地后网络。
    """

    #: 策略名（写入 trace，便于审计）
    name: str = ""
    #: 命中后写入 identity_source 的来源标记
    source: str = IdentitySource.UNKNOWN
    #: 代价，越小越先跑
    cost: int = 1
    #: 命中时的可信度
    confidence: float = 0.5

    def applies(self, q: IdentityQuery) -> bool:  # noqa: D401
        """默认所有输入都尝试。"""
        return True

    @abstractmethod
    def run(self, q: IdentityQuery, ctx: "ResolveContext") -> Optional[PrincipalIdentity]:
        """执行解析，返回部分身份或 ``None``。"""
        raise NotImplementedError

    # 便捷构造：让子类少写样板
    def build(self, **fields: Any) -> PrincipalIdentity:
        ident = PrincipalIdentity(**fields)
        ident.identity_source = self.source
        ident.confidence = self.confidence
        ident.trace = [self.name or self.__class__.__name__]
        return ident


@dataclass
class ResolveContext:
    """策略执行上下文：HTTP 客户端 + 已合并的中间结果 + 逐步日志。"""

    fetch: HttpFetcher
    partial: PrincipalIdentity
    log: List[Dict[str, Any]] = field(default_factory=list)
    timeout: int = 10
    #: 本次解析的原始输入（策略/校验需要回看用户到底给了什么）
    query: Optional["IdentityQuery"] = None

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        """发一次 GET 并记录到 log（供最终实验记录/排障用）。"""
        resp = self.fetch(url, headers=headers, timeout=self.timeout)
        self.log.append({
            "url": url, "status": resp.status, "final_url": resp.url,
            "bytes": len(resp.text or ""), "error": resp.error,
        })
        return resp


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
class IdentityCache:
    """带 TTL 的身份缓存（JSON 文件 + 内存），线程安全，IO 失败自动退化为纯内存。

    身份解析要打好几次外部请求，而 principalId 是账号级稳定标识，
    每轮监控都重解一遍既慢又徒增风控命中。缓存把它降到「首次一次 + TTL 到期一次」。

    文件结构::

        {"version": 1, "items": {"<platform>:<key>": {<PrincipalIdentity>}}}
    """

    VERSION = 1

    def __init__(self, path: Optional[str] = None,
                 ttl: float = DEFAULT_IDENTITY_TTL) -> None:
        self.path = path or ""
        self.ttl = float(ttl)
        self._lock = threading.RLock()
        self._items: Dict[str, PrincipalIdentity] = {}
        self._dirty = False
        self._loaded = False

    # ---- 载入 / 落盘（均 Fail Soft）----
    def load(self) -> "IdentityCache":
        with self._lock:
            if self._loaded:
                return self
            self._loaded = True
            if not self.path or not os.path.exists(self.path):
                return self
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                for key, raw in (data.get("items") or {}).items():
                    ident = PrincipalIdentity.from_dict(raw)
                    if ident is not None:
                        self._items[str(key)] = ident
            except Exception as e:  # noqa: BLE001
                logger.warning("[identity] 缓存载入失败（退化为空缓存）: %s", e)
            return self

    def save(self) -> bool:
        """落盘；无 path 或写失败返回 False（不影响主流程）。"""
        with self._lock:
            if not self.path or not self._dirty:
                return False
            payload = {
                "version": self.VERSION,
                "items": {k: v.to_dict() for k, v in self._items.items()},
            }
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._dirty = False
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("[identity] 缓存落盘失败（仅内存生效）: %s", e)
                return False

    # ---- 读写 ----
    @staticmethod
    def _norm(key: Any) -> str:
        """键归一化：大小写/空白不敏感，避免同一账号在缓存里裂成多条。"""
        return str(key or "").strip().lower()

    def get(self, key: str, now: Optional[float] = None) -> Optional[PrincipalIdentity]:
        """取缓存；过期或未解析成功的条目视为未命中。"""
        self.load()
        with self._lock:
            ident = self._items.get(self._norm(key))
            if ident is None:
                return None
            if not ident.is_resolved() or ident.is_expired(self.ttl, now):
                return None
            return ident

    def put(self, key: str, ident: Optional[PrincipalIdentity]) -> None:
        if ident is None or not ident.is_resolved():
            return
        if not ident.resolved_at:  # 未打时间戳的条目会被判永久过期，这里补上
            ident.touch()
        self.load()
        with self._lock:
            self._items[self._norm(key)] = ident
            # 同一账号可能被多种输入命中，用 principal_id 建二级索引方便反查
            alias = self._norm(f"{ident.platform}:{ident.principal_id}")
            self._items.setdefault(alias, ident)
            self._dirty = True

    def invalidate(self, key: str) -> None:
        self.load()
        with self._lock:
            if self._norm(key) in self._items:
                self._items.pop(self._norm(key), None)
                self._dirty = True

    def __len__(self) -> int:
        self.load()
        return len(self._items)


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------
@dataclass
class ResolveResult:
    """一次解析的完整结果（含过程），供实验记录与 tracking 写回。"""

    identity: Optional[PrincipalIdentity] = None
    hit_cache: bool = False
    tried: List[str] = field(default_factory=list)
    http_log: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    #: 即使没解出主键，沿途收集到的信息（昵称、开播态等）也保留下来给调用方用
    partial: Optional[PrincipalIdentity] = None

    @property
    def ok(self) -> bool:
        return self.identity is not None and self.identity.is_resolved()


class IdentityResolver(ABC):
    """身份解析器基类（模板方法）。

    子类只需提供 ``platform``、``strategies()`` 与（可选）``classify()`` /
    ``verify()``，流水线由基类固定：

        分类输入 → 查缓存 → 按 cost 逐策略解析并合并 → 完整即停 → 校验 → 落缓存

    **Fail Soft 是硬约束**：``resolve()`` 捕获一切异常并返回 ``None``；
    任何单条策略炸掉都只跳过该策略，不影响其余策略与整轮监控。
    """

    platform: str = ""
    ttl: float = DEFAULT_IDENTITY_TTL
    timeout: int = 10
    #: 未通过校验（UNKNOWN）的身份缓存多久 —— 短到下一轮监控就会重验
    unverified_ttl: float = 600.0

    def __init__(self, fetch: Optional[HttpFetcher] = None,
                 cache: Optional[IdentityCache] = None,
                 ttl: Optional[float] = None,
                 timeout: Optional[int] = None) -> None:
        self.fetch: HttpFetcher = fetch or http_fetch
        self.cache = cache if cache is not None else IdentityCache(ttl=ttl or self.ttl)
        if ttl is not None:
            self.ttl = float(ttl)
        if timeout is not None:
            self.timeout = int(timeout)
        #: 最近一次解析的过程记录（排障用）
        self.last_result: Optional[ResolveResult] = None

    # ---- 子类扩展点 ----
    @abstractmethod
    def strategies(self) -> List[ResolveStrategy]:
        """返回本平台的解析策略列表（顺序无所谓，基类按 cost 排序）。"""
        raise NotImplementedError

    def classify(self, raw: str) -> str:  # noqa: D401
        """判定输入类型，默认 UNKNOWN（子类按平台 URL/ID 形态覆写）。"""
        return IdentityKind.UNKNOWN

    def is_complete(self, ident: PrincipalIdentity) -> bool:
        """判定「够用了，可以停」。默认解出主键即够用。"""
        return ident.is_resolved()

    def verify(self, ident: PrincipalIdentity, ctx: ResolveContext) -> Any:
        """可选的二次校验（如请求主页确认账号存在）。默认放行。

        返回 :class:`VerifyOutcome` 之一；也兼容返回 bool
        （``True`` → PASS，``False`` → FAIL）。
        """
        return VerifyOutcome.PASS

    def seed_from_hints(self, q: IdentityQuery) -> PrincipalIdentity:
        """用 config 里已有的字段做种子（最高优先级，后续策略只能补空位）。"""
        ident = PrincipalIdentity(platform=self.platform)
        mapping = ("principal_id", "nickname", "unique_name", "share_user_id",
                   "room_id", "live_id", "home_url", "share_url")
        for key in mapping:
            v = q.hint(key)
            if v:
                setattr(ident, key, v)
        if ident.principal_id:
            ident.identity_source = IdentitySource.CONFIG
            ident.confidence = 1.0
            ident.trace = ["config"]
        return ident

    # ---- 主入口（Fail Soft）----
    def resolve(self, raw: Any = "", hints: Optional[Dict[str, Any]] = None,
                force: bool = False) -> Optional[PrincipalIdentity]:
        """解析身份；**任何情况下都不抛异常**，失败返回 ``None``。"""
        try:
            result = self.resolve_detailed(raw, hints=hints, force=force)
            return result.identity if result.ok else None
        except Exception as e:  # noqa: BLE001 —— 最后一道兜底，保证 Fail Soft
            logger.warning("[identity][%s] 解析异常（已 Fail Soft）: %s",
                           self.platform, e)
            return None

    def resolve_detailed(self, raw: Any = "", hints: Optional[Dict[str, Any]] = None,
                         force: bool = False) -> ResolveResult:
        """解析并返回完整过程（缓存命中情况、跑过的策略、HTTP 日志）。"""
        started = time.time()
        q = IdentityQuery(
            raw=str(raw or "").strip(),
            platform=self.platform,
            hints=dict(hints or {}),
        )
        q.kind = self._safe_classify(q.raw)
        result = ResolveResult()

        if not q.raw and not q.hints:
            self.last_result = result
            return result

        # 1) 缓存
        if not force:
            cached = self.cache.get(q.cache_key)
            if cached is not None and self.is_complete(cached):
                result.identity = cached
                result.partial = cached
                result.hit_cache = True
                result.elapsed_ms = int((time.time() - started) * 1000)
                self.last_result = result
                return result

        # 2) 种子（config 显式配置优先）
        partial = self.seed_from_hints(q)
        if not partial.unique_name and q.kind == IdentityKind.UNIQUE_NAME:
            partial.unique_name = q.raw
        ctx = ResolveContext(fetch=self.fetch, partial=partial,
                             timeout=self.timeout, query=q)
        result.partial = partial

        # 3) 逐策略解析（按 cost 升序：先本地推断，后网络请求）
        if not (partial.is_resolved() and self.is_complete(partial)):
            for st in self._ordered_strategies():
                name = st.name or st.__class__.__name__
                try:
                    if not st.applies(q):
                        continue
                    result.tried.append(name)
                    got = st.run(q, ctx)
                except Exception as e:  # noqa: BLE001 —— 单策略失败不影响其它策略
                    logger.debug("[identity][%s] 策略 %s 失败: %s",
                                 self.platform, name, e)
                    continue
                if got is None:
                    continue
                if not got.platform:
                    got.platform = self.platform
                partial.merge(got)
                if self.is_complete(partial):
                    break

        result.http_log = ctx.log

        # 4) 校验 + 落缓存
        if partial.is_resolved() and self.is_complete(partial):
            outcome = self._safe_verify(partial, ctx)
            if outcome == VerifyOutcome.FAIL:
                logger.info("[identity][%s] 解析结果未通过校验，丢弃: %s",
                            self.platform, partial.principal_id)
            else:
                if outcome == VerifyOutcome.UNKNOWN:
                    # 没验成不等于错，但也不能当成验过：短命缓存 + 显式打标，下轮重验
                    partial.ttl = float(self.unverified_ttl)
                    partial.extra["verified"] = False
                else:
                    partial.ttl = 0.0
                    partial.extra["verified"] = True
                partial.touch()
                result.identity = partial
                self.cache.put(q.cache_key, partial)
                self.cache.save()

        result.http_log = ctx.log
        result.elapsed_ms = int((time.time() - started) * 1000)
        self.last_result = result
        return result

    # ---- 内部 ----
    def _ordered_strategies(self) -> List[ResolveStrategy]:
        try:
            sts = list(self.strategies() or [])
        except Exception as e:  # noqa: BLE001
            logger.warning("[identity][%s] 策略列表构建失败: %s", self.platform, e)
            return []
        return sorted(sts, key=lambda s: (int(getattr(s, "cost", 1)),
                                          -float(getattr(s, "confidence", 0))))

    def _safe_classify(self, raw: str) -> str:
        try:
            return self.classify(raw) or IdentityKind.UNKNOWN
        except Exception:  # noqa: BLE001
            return IdentityKind.UNKNOWN

    def _safe_verify(self, ident: PrincipalIdentity, ctx: ResolveContext) -> str:
        """跑校验并归一化成三态；校验器自己炸了算 UNKNOWN（不算通过，也不算解错）。"""
        try:
            outcome = self.verify(ident, ctx)
        except Exception as e:  # noqa: BLE001
            logger.debug("[identity][%s] 校验异常（记为未校验）: %s", self.platform, e)
            return VerifyOutcome.UNKNOWN
        if outcome is True:
            return VerifyOutcome.PASS
        if outcome is False:
            return VerifyOutcome.FAIL
        if outcome in (VerifyOutcome.PASS, VerifyOutcome.UNKNOWN, VerifyOutcome.FAIL):
            return str(outcome)
        return VerifyOutcome.UNKNOWN


# --------------------------------------------------------------------------
# 凭证三级降级
# --------------------------------------------------------------------------
class CredentialLevel:
    """凭证等级：能用弱的就别用强的，降低账号暴露风险。"""

    ANONYMOUS = "anonymous"   # L1 裸请求
    DEVICE = "did"            # L2 设备标识（匿名可生成）
    COOKIE = "cookie"         # L3 登录态

    ORDER = (ANONYMOUS, DEVICE, COOKIE)


@dataclass
class LadderAttempt:
    """一次凭证尝试的记录。"""

    level: str = ""
    ok: bool = False
    detail: str = ""


@dataclass
class LadderResult:
    """阶梯执行结果：谁成功了、试过哪些、失败原因。"""

    value: Any = None
    level_used: str = ""
    ok: bool = False
    attempts: List[LadderAttempt] = field(default_factory=list)
    last_error: Optional[BaseException] = None

    @property
    def levels_tried(self) -> List[str]:
        return [a.level for a in self.attempts]


class CredentialLadder:
    """凭证三级降级执行器：L1 匿名 → L2 did → L3 Cookie，命中即止。

    用法::

        ladder = CredentialLadder(did=..., cookie=...)
        r = ladder.run(lambda headers, level: call_api(headers))
        if r.ok:
            tracking["cookie_used"] = r.level_used == CredentialLevel.COOKIE

    ``fn`` 抛异常或返回 ``None`` 视为该级失败，自动降级到下一级；
    全部失败时 ``ok=False``，由调用方决定记 gated 还是跳过（Fail Soft）。
    ``should_retry`` 可自定义「什么算失败」（如快手 ``result=400002`` 是风控挑战）。
    """

    def __init__(self, did: str = "", cookie: str = "",
                 extra_headers: Optional[Dict[str, str]] = None,
                 client_key: str = "") -> None:
        self.did = str(did or "")
        self.cookie = str(cookie or "")
        self.client_key = str(client_key or "")
        self.extra_headers = dict(extra_headers or {})

    def levels(self) -> List[Tuple[str, Dict[str, str]]]:
        """可用等级及其请求头（缺凭证的等级自动跳过）。"""
        out: List[Tuple[str, Dict[str, str]]] = [
            (CredentialLevel.ANONYMOUS, dict(self.extra_headers))
        ]
        if self.did:
            hdr = dict(self.extra_headers)
            cookie = f"did={self.did}"
            if self.client_key:
                cookie += f"; client_key={self.client_key}"
            hdr["Cookie"] = cookie
            out.append((CredentialLevel.DEVICE, hdr))
        if self.cookie:
            hdr = dict(self.extra_headers)
            hdr["Cookie"] = self.cookie
            out.append((CredentialLevel.COOKIE, hdr))
        return out

    def run(self, fn: Callable[[Dict[str, str], str], Any],
            should_retry: Optional[Callable[[Any], bool]] = None) -> LadderResult:
        """逐级尝试 ``fn(headers, level)``，返回首个成功结果。永不抛异常。"""
        result = LadderResult()
        for level, headers in self.levels():
            try:
                value = fn(headers, level)
            except Exception as e:  # noqa: BLE001 —— 该级失败即降级
                result.attempts.append(LadderAttempt(level=level, ok=False,
                                                     detail=f"{type(e).__name__}: {e}"))
                result.last_error = e
                continue
            failed = value is None or (should_retry(value) if should_retry else False)
            if failed:
                result.attempts.append(LadderAttempt(level=level, ok=False,
                                                     detail="结果被判定为无效/风控"))
                continue
            result.attempts.append(LadderAttempt(level=level, ok=True))
            result.value = value
            result.level_used = level
            result.ok = True
            return result
        return result
