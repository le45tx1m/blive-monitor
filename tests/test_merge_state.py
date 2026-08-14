"""merge_state 单元测试：验证本地与远端状态文件的语义合并逻辑。

核心场景：CI 持久化失败后，远端有本地丢失的去重记录 → 合并后恢复。
"""
import json
import os

import pytest

# merge_state 不在 tests/ 的 sys.path 中，需要手动导入
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "merge_state",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "merge_state.py"),
)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


# ---------- notify_dedup 合并 ----------

def test_dedup_union_of_keys():
    """本地 + 远端取并集，绝不丢失任何去重记录。"""
    local = {"post:A:1": {"ts": 100}, "post:B:2": {"ts": 200}}
    remote = {"post:A:1": {"ts": 90}, "post:C:3": {"ts": 300}}
    merged = ms.merge_notify_dedup(local, remote)
    assert set(merged.keys()) == {"post:A:1", "post:B:2", "post:C:3"}


def test_dedup_earliest_ts_wins():
    """同一 key 保留更早的 ts（首次推送时间）。"""
    local = {"post:A:1": {"ts": 100}}
    remote = {"post:A:1": {"ts": 90}}
    merged = ms.merge_notify_dedup(local, remote)
    assert merged["post:A:1"]["ts"] == 90


def test_dedup_local_only_preserved():
    local = {"post:B:2": {"ts": 200}}
    remote = {}
    merged = ms.merge_notify_dedup(local, remote)
    assert "post:B:2" in merged


def test_dedup_remote_only_merged():
    """远端独有的 key 必须合并进来（CI 持久化失败恢复的核心）。"""
    local = {}
    remote = {"post:C:3": {"ts": 300}}
    merged = ms.merge_notify_dedup(local, remote)
    assert "post:C:3" in merged


def test_dedup_empty_both():
    assert ms.merge_notify_dedup({}, {}) == {}


# ---------- post_tracking 合并 ----------

def test_tracking_newer_baseline_wins():
    """每个账号取基线更新的那份（aweme_id 数值更大）。"""
    local = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "100", "mode": "api", "nickname": "A"}}
    remote = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "050", "mode": "api", "nickname": ""}}
    merged = ms.merge_post_tracking(local, remote)
    assert merged["douyin_A"]["latest_aweme_id"] == "100"


def test_tracking_remote_only_merged():
    local = {}
    remote = {"douyin_C": {"sec_uid": "S", "latest_aweme_id": "300", "mode": "api", "nickname": "C"}}
    merged = ms.merge_post_tracking(local, remote)
    assert "douyin_C" in merged
    assert merged["douyin_C"]["latest_aweme_id"] == "300"


def test_tracking_preserves_nickname():
    local = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "100", "mode": "api", "nickname": "阿伟"}}
    remote = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "050", "mode": "api", "nickname": ""}}
    merged = ms.merge_post_tracking(local, remote)
    assert merged["douyin_A"]["nickname"] == "阿伟"


def test_tracking_count_mode_comparison():
    """count 模式：取更大的 count 值。"""
    local = {"douyin_D": {"sec_uid": "S", "latest_aweme_id": "count:64", "mode": "count", "latest_ct": 64}}
    remote = {"douyin_D": {"sec_uid": "S", "latest_aweme_id": "count:63", "mode": "count", "latest_ct": 63}}
    merged = ms.merge_post_tracking(local, remote)
    assert merged["douyin_D"]["latest_aweme_id"] == "count:64"


def test_tracking_remote_newer_wins():
    """远端基线更新时取远端。"""
    local = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "100", "mode": "api"}}
    remote = {"douyin_A": {"sec_uid": "S", "latest_aweme_id": "200", "mode": "api", "nickname": "新名"}}
    merged = ms.merge_post_tracking(local, remote)
    assert merged["douyin_A"]["latest_aweme_id"] == "200"
    assert merged["douyin_A"]["nickname"] == "新名"


# ---------- post_rooms 合并 ----------

def test_rooms_local_only_dropped_membership_remote_authoritative():
    """成员以远端为准：本地 checkout 是 run 开始时的快照，不能把远端已删的账号带回来。"""
    local = [{"id": "A", "name": "A", "sec_uid": "SA"}]
    remote = [{"id": "B", "name": "B", "sec_uid": "SB"}]
    merged = ms.merge_post_rooms(local, remote)
    ids = {r["id"] for r in merged}
    assert ids == {"B"}


def test_rooms_deletion_respected():
    """用户中途在 web 删除账号（远端已无），CI 持久化不得复活它。

    2026-08「前端删除时好时坏」根因之一：旧并集逻辑把本地快照里的被删账号
    又合并回去，删除五分钟后随 CI 提交复活。
    """
    local = [{"platform": "douyin", "id": "A", "name": "A"},
             {"platform": "kuaishou", "id": "K1", "name": "K1"}]
    remote = [{"platform": "kuaishou", "id": "K1", "name": "K1"}]  # A 已被用户删除
    merged = ms.merge_post_rooms(local, remote)
    assert len(merged) == 1
    assert merged[0]["id"] == "K1"


def test_rooms_remote_additions_kept():
    """用户中途在 web 新增账号（远端有、本地快照无）必须保留。"""
    local = [{"platform": "douyin", "id": "A", "name": "A"}]
    remote = [{"platform": "douyin", "id": "A", "name": "A"},
              {"platform": "douyin", "id": "NEW", "name": "NEW"}]
    merged = ms.merge_post_rooms(local, remote)
    assert {r["id"] for r in merged} == {"A", "NEW"}


def test_rooms_sec_uid_filled_from_local():
    local = [{"id": "A", "name": "A", "sec_uid": "SA"}]
    remote = [{"id": "A", "name": "old", "sec_uid": ""}]
    merged = ms.merge_post_rooms(local, remote)
    a = next(r for r in merged if r["id"] == "A")
    assert a["sec_uid"] == "SA"


def test_rooms_sec_uid_filled_from_remote():
    local = [{"id": "A", "name": "A", "sec_uid": ""}]
    remote = [{"id": "A", "name": "old", "sec_uid": "SA"}]
    merged = ms.merge_post_rooms(local, remote)
    a = next(r for r in merged if r["id"] == "A")
    assert a["sec_uid"] == "SA"


def test_rooms_name_remote_wins_when_nonempty():
    """用户可能中途改名：远端 name 非空时优先，不被本地旧快照回写。"""
    local = [{"id": "A", "name": "旧名", "sec_uid": "SA"}]
    remote = [{"id": "A", "name": "新名", "sec_uid": "SA"}]
    merged = ms.merge_post_rooms(local, remote)
    assert merged[0]["name"] == "新名"


def test_rooms_name_local_fills_blank():
    local = [{"id": "A", "name": "本地解析的昵称", "sec_uid": "SA"}]
    remote = [{"id": "A", "name": "", "sec_uid": "SA"}]
    merged = ms.merge_post_rooms(local, remote)
    assert merged[0]["name"] == "本地解析的昵称"


def test_rooms_platform_aware_key():
    """同 id 不同 platform 是两个账号：按 platform|id 匹配，互不串扰。"""
    local = [{"platform": "douyin", "id": "X", "name": "dx", "sec_uid": "S1"},
             {"platform": "kuaishou", "id": "X", "name": "kx-old"}]
    remote = [{"platform": "douyin", "id": "X", "name": "dx", "sec_uid": ""}]
    merged = ms.merge_post_rooms(local, remote)
    # kuaishou|X 不在远端 → 被删；douyin|X 保留并富化 sec_uid
    assert len(merged) == 1
    assert merged[0]["platform"] == "douyin"
    assert merged[0]["sec_uid"] == "S1"


# ---------- history 合并 ----------

def test_history_dedup_by_time_name():
    local = [{"time": "2025-01-01 10:00", "name": "A", "platform": "douyin"}]
    remote = [
        {"time": "2025-01-01 09:00", "name": "C", "platform": "douyin"},
        {"time": "2025-01-01 10:00", "name": "A", "platform": "douyin"},  # 重复
    ]
    merged = ms.merge_history(local, remote)
    assert len(merged) == 2


def test_history_capped():
    local = [{"time": f"2025-01-01 {i:02d}:00", "name": str(i), "platform": "douyin"} for i in range(600)]
    merged = ms.merge_history(local, [])
    assert len(merged) <= ms.HISTORY_MAX


# ---------- history 合并透传 rid（日志模块重构） ----------

def test_history_passthrough_rid():
    """merge_history 以 dict 原样透传，新增 rid 字段随条目保留（与 check_status 写入结构兼容）。"""
    local = [{"time": "t1", "name": "A", "platform": "douyin", "rid": "R1", "title": "x"}]
    remote = [{"time": "t0", "name": "B", "platform": "bilibili", "rid": "R2", "title": "y"}]
    merged = ms.merge_history(local, remote)
    assert len(merged) == 2
    assert all("rid" in e for e in merged)


def test_history_max_imported_from_log_utils():
    """HISTORY_MAX 单一来源：merge_state 引用 log_utils.HISTORY_MAX。"""
    import log_utils
    assert ms.HISTORY_MAX == 500
    assert ms.HISTORY_MAX is log_utils.HISTORY_MAX


# ---------- history 合并透传新增分级字段（日志模块功能性重写） ----------

def test_history_passthrough_typed_fields():
    """带 type/level/detail/account 的 history 经 merge_history 字段无损透传。"""
    local = [{
        "time": "t1", "name": "A", "platform": "douyin", "rid": "R1",
        "type": "new_post", "level": "info", "detail": "作品x", "account": "R1",
    }]
    remote = [{
        "time": "t0", "name": "B", "platform": "bilibili", "rid": "R2",
        "type": "live_on", "level": "info", "detail": "", "account": "R2",
    }]
    merged = ms.merge_history(local, remote)
    assert len(merged) == 2
    assert all("type" in e and "level" in e for e in merged)
    assert all("detail" in e and "account" in e for e in merged)


def test_history_merge_with_type_is_idempotent():
    """带 type 的 history 经并集合并后，type 字段不丢失、条数正确。"""
    local = [{"time": "t1", "name": "A", "platform": "douyin", "type": "new_post", "rid": "R1"}]
    remote = [{"time": "t1", "name": "A", "platform": "douyin", "type": "new_post", "rid": "R1"}]
    merged = ms.merge_history(local, remote)
    assert len(merged) == 1  # 同 time+name+platform 去重
    assert merged[0]["type"] == "new_post"


# ---------- 合并后孤儿裁剪（删除彻底清理） ----------

def test_tracking_keys_from_rooms():
    rooms = [
        {"platform": "kuaishou", "id": "K1"},
        {"id": "D1"},                      # 无 platform → douyin 约定
        {"platform": "douyin", "id": "D2"},
        {"platform": "douyin"},            # 无 id → 忽略
    ]
    assert ms.tracking_keys_from_rooms(rooms) == {
        "kuaishou_K1", "douyin_D1", "douyin_D2"}


def test_history_keys_from_rooms():
    rooms = [{"platform": "kuaishou", "id": "K1"}, {"id": "D1"}]
    assert ms.history_keys_from_rooms(rooms) == {"kuaishou|K1", "douyin|D1"}


def test_prune_merged_history():
    history = [
        {"rid": "A", "platform": "douyin", "time": "t1"},    # 活跃 → 保留
        {"rid": "GONE", "platform": "douyin", "time": "t2"},  # 已删 → 裁掉
        {"time": "t3", "type": "system"},                     # 无 rid 存量 → 保守保留
    ]
    out = ms.prune_merged_history(history, {"douyin|A"})
    assert len(out) == 2
    assert all(e.get("rid") != "GONE" for e in out)


def test_main_合并后彻底清理已删账号(tmp_path):
    """端到端：CI run 中途用户删了账号 —— 本地是 run 开始时的旧快照（还含被删
    账号的状态），远端（master，web 直写）已无该账号。合并后被删账号的
    tracking/history 必须清掉，不得被并集复活（2026-08「删号后缓存残留」根因）。"""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    def w(name, obj):
        (repo / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    # 远端（HEAD）= 用户删除后的最新状态：只剩 A
    w("post_rooms.json", [{"platform": "kuaishou", "id": "A", "name": "A"}])
    w("post_tracking.json", {"kuaishou_A": {"latest_post_id": "p1"}})
    w("history.json", [{"rid": "A", "platform": "kuaishou", "time": "t1", "name": "A"}])
    w("notify_dedup.json", {})
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "remote state after deletion"], cwd=repo, check=True)

    # 本地 = CI run 的旧快照：run 开始时 GONE 还在，本轮还处理了它
    w("post_rooms.json", [{"platform": "kuaishou", "id": "A", "name": "A"},
                          {"platform": "kuaishou", "id": "GONE", "name": "GONE"}])
    w("post_tracking.json", {"kuaishou_A": {"latest_post_id": "p1"},
                             "kuaishou_GONE": {"latest_post_id": "p2"}})
    w("history.json", [{"rid": "A", "platform": "kuaishou", "time": "t1", "name": "A"},
                       {"rid": "GONE", "platform": "kuaishou", "time": "t2", "name": "GONE"}])

    import sys
    argv_saved = sys.argv
    sys.argv = ["merge_state.py", "HEAD", "--repo", str(repo)]
    try:
        assert ms.main() == 0
    finally:
        sys.argv = argv_saved

    merged_rooms = json.loads((repo / "post_rooms.json").read_text(encoding="utf-8"))
    merged_tracking = json.loads((repo / "post_tracking.json").read_text(encoding="utf-8"))
    merged_history = json.loads((repo / "history.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in merged_rooms} == {"A"}
    assert set(merged_tracking.keys()) == {"kuaishou_A"}, \
        f"被删账号的 tracking 复活了: {list(merged_tracking.keys())}"
    assert all(e.get("rid") != "GONE" for e in merged_history), \
        "被删账号的 history 复活了"
