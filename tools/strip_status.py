#!/usr/bin/env python3
"""仅在 status.json 的「meaningful 内容」相对 HEAD 有变化时才纳入提交。

背景：check_status.py 每轮都会重写 status.json，并带上 ``updated`` 时间戳以及
每个房间的 ``time`` / ``live_duration`` 等易变字段，导致文件永远有 diff → 每 5 分钟
必推一条空 commit（CI 刷屏）。本脚本在 CI 的「Persist state」步骤里运行，剥离这些
纯时间戳字段后比对 HEAD，仅当 meaningful 内容真的变化时才 ``git add -f status.json``。

幂等可重跑：缺失 status.json 或 HEAD 无 status.json 时安全降级（直接 add）。
"""
import json
import subprocess
import sys

STATUS = "status.json"


def strip_volatile(d):
    """去掉每轮必变的时间戳字段，保留 meaningful 内容用于比对。"""
    if not isinstance(d, dict):
        return d
    d = {k: v for k, v in d.items() if k != "updated"}
    rooms = d.get("rooms")
    if isinstance(rooms, list):
        for r in rooms:
            if isinstance(r, dict):
                r.pop("time", None)
                r.pop("live_duration", None)
    return d


def load_local(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_head(path):
    try:
        out = subprocess.check_output(
            ["git", "show", f"HEAD:{path}"], stderr=subprocess.DEVNULL
        )
        return json.loads(out.decode("utf-8"))
    except Exception:
        return None


def main():
    cur = strip_volatile(load_local(STATUS))
    head = strip_volatile(load_head(STATUS))
    if cur != head:
        subprocess.run(["git", "add", "-f", STATUS], check=False)
        print("status.json meaningful 内容变化，已 git add -f")
    else:
        print("status.json 仅时间戳变化，跳过提交")


if __name__ == "__main__":
    sys.exit(main())
