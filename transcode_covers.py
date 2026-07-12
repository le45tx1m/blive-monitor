"""D1 封面转存：将抖音新作封面从外部 CDN 转存到仓库内 ``assets/covers/``，
改写 ``post_tracking[id].latest_cover`` 为仓库 raw URL，规避抖音防盗链破图。

设计要点（与阶段二 2a 架构设计一致）：
- **零新增依赖**：仅使用标准库 ``urllib.request``（检测脚本用 Playwright 抓封面源 URL，
  本模块只负责下载，不引入 requests）。
- **差异提交**：仅当「封面缺失」或「latest_aweme_id 相对 manifest 变更」时才下载；
  manifest（``<covers_dir>/.manifest.json``，结构 ``{id:{aweme_id, sha256}}``）用于判定，
  避免每次 CI 重写同一封面导致仓库膨胀。
- **下载失败不阻塞**：``download_cover`` 失败返回 ``False``，保留原 CDN URL，下轮重试。
- **CDN 源持久化**：``check_new_posts`` 会把作品封面 CDN URL 另存到 ``latest_cover_cdn``
  （``latest_cover`` 本会被改写为仓库 raw URL、源信息丢失）。本模块优先用 ``latest_cover_cdn``
  作为下载源，使「新作品封面晚到 / 刷新」时能重新下载到最新封面，而非永远停在旧图。

前端 ``monitor.html`` 已渲染 ``tt.latest_cover``（line ~2129 + ``onerror`` 兜底），
本模块只改写该字段为仓库内 raw URL，前端零改动。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from typing import Any, Dict, Optional


def _raw_url(owner: str, repo: str, branch: str, covers_dir: str, key: str) -> str:
    """构造仓库内封面的 raw.githubusercontent.com URL。"""
    covers_dir = covers_dir.strip("/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{covers_dir}/{key}.jpg"


def download_cover(url: str, dest: str, timeout: int = 15) -> bool:
    """用标准库 urllib 下载封面到 dest。

    失败返回 ``False``（不阻塞，下轮重试）。成功返回 ``True``。
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "blive-monitor-cover-transcoder"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 (CI 内部固定来源)
            data = resp.read()
        if not data:
            return False
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        # 下载失败（网络/防盗链/超时等）：返回 False，不抛异常，下轮重试。
        return False


def rewrite_latest_cover(t: Dict[str, Any], raw_url: str) -> None:
    """将 post_tracking 条目 t 的 latest_cover 改写为仓库内 raw URL。"""
    t["latest_cover"] = raw_url


def _sha256(path: str) -> Optional[str]:
    """计算文件 sha256（用于 manifest），失败返回 None。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _cover_base(url: str) -> str:
    """封面源 URL 归一化：path 标识图片本身，query 签名可能每次变化，仅比 path 判断是否为同一张图。"""
    return (url or "").split("?")[0]


def transcode_all(
    tracking: Dict[str, dict],
    manifest: Dict[str, dict],
    owner: str,
    repo: str,
    branch: str,
    covers_dir: str = "assets/covers",
) -> Dict[str, Any]:
    """遍历 post_tracking，对 latest_cover 存在且（封面缺失或 aweme_id 变更）的账号，
    下载封面到 ``covers_dir/<key>.jpg``，改写 ``latest_cover`` 为 raw URL。

    :param tracking: post_tracking.json 内容（dict by key，如 ``douyin_601914453``）
    :param manifest: 既有 ``.manifest.json``（``{id:{aweme_id, sha256}}``），可为空
    :param owner/repo/branch: 仓库坐标（如 racheko-lab / blive-monitor / master）
    :param covers_dir: 仓库内封面目录（相对仓库根，默认 ``assets/covers``）
    :return: ``{tracking, manifest, changed, downloaded, total}``
    """
    manifest = dict(manifest or {})
    changed = 0
    downloaded = 0
    total = 0
    abs_covers = covers_dir  # 相对 CI checkout 工作目录（仓库根）

    for key, t in tracking.items():
        if not isinstance(t, dict):
            continue
        raw_url = _raw_url(owner, repo, branch, covers_dir, key)
        # 有效 CDN 源：优先用 check_new_posts 持久化下来的 latest_cover_cdn
        # （latest_cover 会被本模块改写成仓库 raw URL、源信息丢失，故必须另存 CDN 源）。
        # 兜底：若 latest_cover_cdn 缺失，但 latest_cover 仍是 CDN URL（尚未被改写），也可用。
        cdn = t.get("latest_cover_cdn")
        if not (isinstance(cdn, str) and cdn.startswith("http")):
            src0 = t.get("latest_cover")
            cdn = src0 if (isinstance(src0, str) and not src0.startswith("https://raw.githubusercontent.com")) else None
        if not cdn:
            # 既无 CDN 源、也无可用封面：若 latest_cover 已是 raw，仅防御性确保 manifest 一致；否则跳过
            src0 = t.get("latest_cover")
            if isinstance(src0, str) and src0.startswith("https://raw.githubusercontent.com"):
                rec = dict(manifest.get(key, {}))
                if rec.get("cover_url") != src0 or rec.get("aweme_id") != t.get("latest_aweme_id"):
                    rec["aweme_id"] = t.get("latest_aweme_id")
                    rec["cover_url"] = src0
                    manifest[key] = rec
                    changed += 1
            continue
        total += 1
        aweme_id = t.get("latest_aweme_id")
        cover_path = os.path.join(abs_covers, f"{key}.jpg")
        prev = manifest.get(key, {})
        # 重新下载判定：封面文件缺失 / 作品更新(latest_aweme_id 变更) / 尚无 manifest 记录 /
        # 封面源 URL 变更（同一作品封面“晚到”或 URL 刷新，aweme_id 未变但源 URL 已变，
        # 必须重新下载，否则只把 latest_cover 指针改回 raw、却不更新图片，导致封面永远停在旧图）。
        need = (
            (not os.path.exists(cover_path))
            or (prev.get("aweme_id") is None)
            or (prev.get("aweme_id") != aweme_id)
            or (_cover_base(prev.get("cover_url", "")) != _cover_base(cdn))
        )
        if need:
            if download_cover(cdn, cover_path):
                # 仅改写 latest_cover 指针为仓库 raw URL；latest_cover_cdn 保持不变（下次仍可溯源刷新）
                rewrite_latest_cover(t, raw_url)
                manifest[key] = {
                    "aweme_id": aweme_id,
                    "sha256": _sha256(cover_path),
                    "cover_url": cdn,
                }
                changed += 1
                downloaded += 1
            else:
                # 下载失败：保留原 CDN 源，下轮重试；不写入 manifest
                continue
        else:
            # 封面已存在且未变更：防御性确保 latest_cover 指向仓库 raw URL
            # （防止 check_new_posts.py 每轮回填 CDN URL 导致前端破图）
            if not str(t.get("latest_cover", "")).startswith(raw_url):
                rewrite_latest_cover(t, raw_url)
                changed += 1
    return {
        "tracking": tracking,
        "manifest": manifest,
        "changed": changed,
        "downloaded": downloaded,
        "total": total,
    }


def main(argv: Optional[list] = None) -> int:
    """CLI：读取 post_tracking.json + manifest，转存封面，写回变更。"""
    parser = argparse.ArgumentParser(
        description="Transcode douyin new-post covers into repo assets/covers"
    )
    parser.add_argument("--owner", default="racheko-lab")
    parser.add_argument("--repo", default="blive-monitor")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--covers-dir", default="assets/covers")
    parser.add_argument("--tracking", default="post_tracking.json")
    parser.add_argument("--manifest", default=None,
                        help="manifest 路径（默认 <covers-dir>/.manifest.json）")
    args = parser.parse_args(argv)

    manifest_path = args.manifest or os.path.join(args.covers_dir, ".manifest.json")

    try:
        with open(args.tracking, "r", encoding="utf-8") as f:
            tracking = json.load(f)
    except FileNotFoundError:
        tracking = {}

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        manifest = {}

    res = transcode_all(
        tracking, manifest, args.owner, args.repo, args.branch, args.covers_dir
    )

    if res["changed"]:
        with open(args.tracking, "w", encoding="utf-8") as f:
            json.dump(res["tracking"], f, ensure_ascii=False, indent=2)
        os.makedirs(args.covers_dir, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(res["manifest"], f, ensure_ascii=False, indent=2)

    print(
        f"[transcode_covers] total={res['total']} "
        f"changed={res['changed']} downloaded={res['downloaded']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
