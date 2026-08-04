#!/usr/bin/env python3
"""
通知去重账本（直播监控 / 新作品监控共用）

为什么需要（重复推送的根因与防线）
--------------------------------
两个监控脚本原本只靠 Git 持久化的状态文件（state.json / tracking.json /
post_tracking.json）做去重：开播后写 "live"，下一轮读到 prev=live 就不再推送。
但这一机制有两个脆弱点，都会导致「同一条通知被重复发送」：

  1) 状态持久化偶发失败：CI 的 Persist 步骤若因网络/分叉未能把状态文件 push 回
     仓库，下一轮 checkout 到的是旧基线，会把同一次开播当成「首次检测」重新推送；
  2) 抖音直播页在 CI 无登录态下偶尔抓取失败，会退化返回 "offline"
     （check_status.fetch_douyin 的兜底分支），造成 live→offline→live 的「闪烁」，
     触发重复的「开播」通知。

本模块提供与状态持久化解耦的独立去重账本（notify_dedup.json），作为第二道防线：

  - 直播 / 回放开播：key = "live:{platform}_{rid}"，冷却 LIVE_COOLDOWN_SECONDS（默认 2h）。
    连续直播期间 prev_status=live 本就不会重复推送；冷却主要吸收「闪烁」造成的
    假离线→真开播，以及状态文件短暂丢失后的重复首检。
  - 新作品：key = "post:{sec_uid}:{aweme_id}"，永久不重复（同一作品只推一次）。
    退化计数模式：key = "post:{sec_uid}:count:{count}"，永久不重复。

账本本身也由 CI 持久化（git add -f notify_dedup.json），因此跨 run 有效；
即便单次持久化失败，至多只会在冷却窗口后补推一次，不会形成刷屏。
"""

import math
import os
import time
from typing import Any, Dict, Optional

# 复用公共 JSON 读写（原子写，避免半截文件）
try:
    from common import load_json_file, save_json_file
except Exception:  # 允许单独 import / 单测时降级
    import json as _json

    def load_json_file(filepath: str, default: Any = None) -> Any:
        if default is None:
            default = {}
        if not os.path.exists(filepath):
            return default
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return default

    def save_json_file(filepath: str, data: Any) -> None:
        tmp = f"{filepath}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)


REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(REPO_DIR, "notify_dedup.json")

# 直播通知冷却（秒）：2 小时内同一房间的开播通知只发一次（吸收闪烁 / 状态丢失）
LIVE_COOLDOWN_SECONDS = 7200

# 新作品去重：永久（key 已记录则永不重推）。用无穷大冷却表达「永久」。
PERMANENT = math.inf

# 直播 key 的最长留存（秒）：超过该时长的 live: key 可被清理，避免账本无限增长。
LIVE_KEY_TTL_SECONDS = 7 * 24 * 3600

# 账本条目上限（超出后保留最近 N 条）；post: key 永不因裁剪而丢弃。
MAX_ENTRIES = 5000


def _load() -> Dict[str, Any]:
    return load_json_file(LEDGER_FILE, {})


def _save(ledger: Dict[str, Any]) -> None:
    save_json_file(LEDGER_FILE, ledger)


def should_notify(key: str, cooldown: float = LIVE_COOLDOWN_SECONDS,
                  now: Optional[float] = None) -> bool:
    """该 key 当前是否应该推送。

    - 未记录过 → 允许（True）
    - 已记录，且距上次发送 ≥ cooldown → 允许（True）
    - 已记录，且距上次发送 < cooldown（含 cooldown=PERMANENT 的永久模式）→ 拒绝（False）

    Args:
        key: 去重键（见模块 docstring 的命名约定）
        cooldown: 冷却秒数；传 PERMANENT（math.inf）表示永久不重复
        now: 可注入的当前时间戳（测试用）
    """
    if not key:
        return True
    now = now if now is not None else time.time()
    ledger = _load()
    entry = ledger.get(key)
    if not entry:
        return True
    try:
        last_ts = float(entry.get("ts", 0))
    except (ValueError, TypeError, AttributeError):
        return True
    return (now - last_ts) >= cooldown


def record(key: str, now: Optional[float] = None) -> None:
    """推送成功后记录该 key 的发送时间（幂等：已存在则刷新时间戳）。

    仅在推送确实成功时调用，避免「推送失败却已标记去重」导致漏报后无法补推。
    """
    if not key:
        return
    now = now if now is not None else time.time()
    ledger = _load()
    ledger[key] = {"ts": now}
    _save(ledger)


def sync_from_remote() -> int:
    """推送前从远端拉取最新 notify_dedup.json 并合并到本地账本。

    解决并发 run 导致的去重失效（根因见模块 docstring 脆弱点 1）：
    GitHub Actions 的 concurrency 偶发不排队（如某 run 卡住 10+ 分钟），
    两个 run 几乎同时 checkout 旧账本。先启动的 run 推送后 record 并
    push 回仓库；后启动的 run 若仅凭 checkout 时的旧账本判断，会重复
    推送。本函数在推送前调用，拉取远端最新账本合并，使后启动的 run
    能看到先完成 run 已 record 的去重条目。

    合并策略：对每个 key 取 max(本地 ts, 远端 ts)（远端条目较新则覆盖）。
    无 token / 网络失败 / 远端无文件时静默降级为仅用本地账本，不阻断流程。

    依赖 CI 自动注入的 GITHUB_TOKEN（权限 contents: read 即可）。
    Returns: 合并后新增/更新的远端条目数（测试用）。
    """
    import base64
    import json as _json
    import urllib.request

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY") or "racheko-lab/blive-monitor"
    branch = os.environ.get("GH_BRANCH") or "master"
    url = (
        f"https://api.github.com/repos/{repo}/contents/notify_dedup.json"
        f"?ref={branch}"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "blive-monitor",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        remote = _json.loads(base64.b64decode(data["content"]))
    except Exception:
        return 0

    if not isinstance(remote, dict):
        return 0

    local = _load()
    merged = 0
    for k, v in remote.items():
        try:
            r_ts = float(v.get("ts", 0))
        except (ValueError, TypeError, AttributeError):
            continue
        local_entry = local.get(k)
        l_ts = float(local_entry.get("ts", 0)) if local_entry else 0.0
        if r_ts > l_ts:
            local[k] = v
            merged += 1

    if merged:
        _save(local)
    return merged


def prune(now: Optional[float] = None) -> None:
    """裁剪账本：

    - 丢弃过期的 live: key（距上次发送超过 LIVE_KEY_TTL_SECONDS）；
    - post: key 永久保留（同一作品只推一次，绝不能因裁剪而重推）；
    - 若仍超过 MAX_ENTRIES，保留最近 N 条。
    """
    now = now if now is not None else time.time()
    ledger = _load()
    if not ledger:
        return

    kept: Dict[str, Any] = {}
    for k, v in ledger.items():
        if k.startswith("live:"):
            try:
                ts = float(v.get("ts", 0))
            except (ValueError, TypeError, AttributeError):
                ts = 0.0
            if (now - ts) < LIVE_KEY_TTL_SECONDS:
                kept[k] = v
            # 过期 live: key 直接丢弃
        else:
            # post: 等其它 key 永久保留
            kept[k] = v

    if len(kept) > MAX_ENTRIES:
        items = sorted(kept.items(), key=lambda kv: kv[1].get("ts", 0))
        kept = dict(items[-MAX_ENTRIES:])

    if len(kept) != len(ledger):
        _save(kept)
