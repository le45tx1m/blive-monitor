#!/usr/bin/env python3
"""
级联清理模块（孤儿记录 / 字段合并）。

纯函数 + common.save_json_file 原子写，不引入额外状态。
所有「孤儿识别」必须基于「重读磁盘当前内容」构造的 active_keys，
绝不使用启动内存副本，以避免与前端增删竞态（防止复活已删账号）。

识别键约定（与 docs/system_design.md §8 一致）：
  - history 孤儿：``f"{platform}|{rid}"``（来自当前磁盘 rooms.json）
  - post_tracking 孤儿：``f"douyin_{rid}"``（来自当前磁盘 post_rooms.json）
"""

import os

import common


def prune_history_orphans(history, active_keys):
    """级联清理 history.json 孤儿：仅保留 ``f"{platform}|{rid}" ∈ active_keys`` 的条目。

    Args:
        history: history.json 内容（list[dict]）。
        active_keys: 活钥集合，元素形如 ``"platform|rid"``（来自当前磁盘 rooms.json）。

    Returns:
        清理后的 history 列表（新对象，不改写入参）。

    Note:
        对无 rid 的存量（历史）条目（本次重构前写入，结构无 rid），因无法用 rid 精确归因，
        一律保留，避免首轮部署即清空全部历史；后续新写入的条目带 rid，可被正确裁剪。
    """
    if not isinstance(history, list):
        return []
    if not isinstance(active_keys, set):
        active_keys = set(active_keys)

    result = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("rid")
        if rid:  # 新结构：按 platform|rid 精确匹配
            key = f"{entry.get('platform', '')}|{rid}"
            if key in active_keys:
                result.append(entry)
        else:  # 存量无 rid：保守保留（无法可靠归因）
            result.append(entry)
    return result


def prune_tracking_orphans(tracking, active_keys):
    """级联清理 tracking 类字典孤儿：删除 ``key ∉ active_keys`` 的账号状态。

    通用实现（仅按 key 集合裁剪），既可用于 post_tracking.json（活钥来自 post_rooms.json），
    也可用于 live tracking.json（活钥来自 rooms.json，形如 ``"{platform}_{rid}"``）。
    两种用法的差异仅在 active_keys 的构造，本函数不参与键格式假设。

    Args:
        tracking: tracking 类字典（post_tracking.json / tracking.json），key 形如 ``"douyin_{rid}"``。
        active_keys: 活钥集合（来自当前磁盘的 rooms.json 或 post_rooms.json）。

    Returns:
        清理后的 tracking 字典（新对象）。
    """
    if not isinstance(tracking, dict):
        return {}
    if not isinstance(active_keys, set):
        active_keys = set(active_keys)
    return {k: v for k, v in tracking.items() if k in active_keys}


def find_orphan_covers(rooms, post_rooms, covers_dir):
    """返回 assets/covers 下、无对应房间（直播或新作品）的孤儿封面完整路径列表。

    封面命名约定：``{platform}_{id}.jpg``（与 transcode_covers 一致，post 缺 platform 时按 douyin 兜底）。
    活房间集合 = rooms.json 的 ``platform_id`` ∪ post_rooms.json 的 ``platform_id``
    （post_rooms 条目缺 platform 按 douyin 兜底，与前端/后端键构造保持一致）。

    Args:
        rooms: rooms.json 内容（list[dict]）。
        post_rooms: post_rooms.json 内容（list[dict]）。
        covers_dir: 封面目录（绝对/相对路径）。

    Returns:
        孤儿封面完整路径列表（无则空列表）。纯函数，不读写磁盘（仅 os.listdir 只读扫描）。
    """
    rooms = rooms if isinstance(rooms, list) else []
    post_rooms = post_rooms if isinstance(post_rooms, list) else []
    live_set = {
        f"{r.get('platform', 'bilibili')}_{r.get('id', '')}"
        for r in rooms if r.get("id")
    }
    post_set = {
        f"{(r.get('platform') or 'douyin')}_{r.get('id', '')}"
        for r in post_rooms if r.get("id")
    }
    active = live_set | post_set

    orphans = []
    if not covers_dir or not os.path.isdir(covers_dir):
        return orphans
    for fn in os.listdir(covers_dir):
        if not fn.endswith(".jpg"):
            continue
        base = fn[:-4]  # 去掉 .jpg
        if base not in active:
            orphans.append(os.path.join(covers_dir, fn))
    return orphans


#: 身份解析可自动补齐的 config 字段（任务八）。
#: 这些字段**只填空位**，绝不覆盖用户手填的值 —— config 表达的是用户意志，
#: 解析结果只是「用户没说时的最佳猜测」。若两者冲突以用户为准，由 resolver 侧告警。
IDENTITY_FILL_FIELDS = (
    "principal_id",      # graphql 真正需要的 userId，解析成本最高、账号级稳定
    "origin_user_id",    # 不可变真身，交叉校验用
    "nickname",          # 通知里显示人话
    "unique_name",       # 快手号
    "home_url",
    "share_url",
    "room_id",
    "identity_source",
)


def merge_post_rooms_fields(config_file, resolved, fill_fields=IDENTITY_FILL_FIELDS):
    """重读磁盘 post_rooms.json，仅对仍存在的账号「原地」更新字段。

    两类字段两种语义：
      - ``sec_uid`` / ``name``：解析结果更权威，有变化就更新（沿用既有行为）；
      - ``fill_fields``（身份字段）：**只填空位**。用户在 config 里写死的东西
        不该被自动解析悄悄改掉，否则「改了配置不生效」会极难排查。

    用本轮解析到的值（resolved）回填磁盘文件中「仍存在的」账号字段：
      - 绝不把内存副本里多出来的账号写回（即不复活前端已删除的账号）；
      - 仅当有字段实际变化时回写，避免无意义提交。

    Args:
        config_file: post_rooms.json 路径。
        resolved: ``{rid: entry}`` 本轮解析/写回过的账号（entry 含最新字段）。
        fill_fields: 只填空位的字段名集合，默认 :data:`IDENTITY_FILL_FIELDS`。

    Returns:
        是否发生了字段变更（bool）。仅在变更时原子写回磁盘。
    """
    current_rooms = common.load_json_file(config_file, []) or []
    if not isinstance(current_rooms, list):
        current_rooms = []
    if not isinstance(resolved, dict):
        resolved = {}

    changed = False
    for entry in current_rooms:
        if not isinstance(entry, dict):
            continue
        rid = str(entry.get("id", ""))
        if not rid:
            continue
        r = resolved.get(rid)
        if not r:
            continue
        new_sec = r.get("sec_uid")
        new_name = r.get("name")
        if new_sec and entry.get("sec_uid") != new_sec:
            entry["sec_uid"] = new_sec
            changed = True
        if new_name and entry.get("name") != new_name:
            entry["name"] = new_name
            changed = True
        for f in (fill_fields or ()):
            val = r.get(f)
            if val and not entry.get(f):
                entry[f] = val
                changed = True

    if changed:
        common.save_json_file(config_file, current_rooms)
    return changed
