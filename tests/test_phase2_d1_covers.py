"""阶段二 2a · D1 封面转存：差异判定 + latest_cover 改写契约。

验证 transcode_covers.transcode_all 的差异提交逻辑与 latest_cover 改写为仓库 raw URL
的契约。下载用 monkeypatch 的伪实现（避免联网），并 spy 调用次数确认「差异提交」
（仅在封面缺失 / aweme_id 变更时下载）。

注意：transcode_all 内部用传入的 covers_dir 拼 raw URL，因此测试构造 latest_cover 时
必须与调用时使用的 covers_dir 完全一致（CI 实际用相对路径 "assets/covers"）。
"""
import os

import transcode_covers as tc

OWNER, REPO, BRANCH = "racheko-lab", "blive-monitor", "master"


def raw_of(cdir, key="douyin_1"):
    return tc._raw_url(OWNER, REPO, BRANCH, cdir, key)


def test_download_cover_returns_false_on_error():
    # 指向一个必然失败的伪 URL（无监听端口），应返回 False 而非抛异常
    ok = tc.download_cover("http://127.0.0.1:0/nope.jpg", "/tmp/should_not_exist_xyz.jpg", timeout=2)
    assert ok is False


def test_rewrite_latest_cover_sets_field():
    t = {"latest_cover": "https://cdn.example.com/x.jpg"}
    tc.rewrite_latest_cover(t, raw_of("assets/covers"))
    assert t["latest_cover"] == raw_of("assets/covers")


def test_transcode_downloads_when_cover_missing(tmp_path, monkeypatch):
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    calls = []

    def fake_download(url, dest, timeout=15):
        calls.append((url, dest))
        assert dest.endswith("douyin_1.jpg")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(b"img")
        return True

    monkeypatch.setattr(tc, "download_cover", fake_download)

    tracking = {"douyin_1": {"latest_cover": "https://cdn.example.com/1.jpg", "latest_aweme_id": "A1"}}
    res = tc.transcode_all(tracking, {}, OWNER, REPO, BRANCH, covers_dir=cdir)

    assert res["downloaded"] == 1
    assert res["changed"] == 1
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")
    assert res["manifest"]["douyin_1"]["aweme_id"] == "A1"
    assert len(calls) == 1


def test_transcode_no_redownload_when_unchanged(tmp_path, monkeypatch):
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    # 预置封面文件（模拟上一轮已转存）
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"img")
    manifest = {"douyin_1": {"aweme_id": "A1", "sha256": "deadbeef", "cover_url": raw_of(cdir, "douyin_1")}}

    monkeypatch.setattr(tc, "download_cover",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应再次下载")) or False)

    # latest_cover 已是 raw URL（与 cdir 一致），manifest 已记录该 raw 源，aweme_id 未变
    # → 不应下载、不应改写（防御性分支发现 manifest 已一致，changed=0）
    tracking = {"douyin_1": {"latest_cover": raw_of(cdir, "douyin_1"), "latest_aweme_id": "A1"}}
    res = tc.transcode_all(tracking, manifest, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 0
    assert res["changed"] == 0
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")


def test_transcode_redownload_when_aweme_changed(tmp_path, monkeypatch):
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"old")
    manifest = {"douyin_1": {"aweme_id": "A1", "sha256": "old"}}

    monkeypatch.setattr(tc, "download_cover", lambda url, dest, timeout=15: True)

    # aweme_id 变更 → 应重新下载
    tracking = {"douyin_1": {"latest_cover": "https://cdn.example.com/new.jpg", "latest_aweme_id": "A2"}}
    res = tc.transcode_all(tracking, manifest, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 1
    assert res["changed"] == 1
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")
    assert res["manifest"]["douyin_1"]["aweme_id"] == "A2"


def test_transcode_defensive_rewrite_when_cdn_reverted(tmp_path, monkeypatch):
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"img")
    manifest = {"douyin_1": {"aweme_id": "A1", "sha256": "x", "cover_url": "https://cdn.example.com/reverted.jpg"}}

    # 不应触发下载：封面存在、aweme_id 未变、且 manifest 记录的封面源与该 CDN 一致
    # （latest_cover 被 check_new_posts 回退为 CDN，但图片本身未变，只防御性改写指针为 raw）
    monkeypatch.setattr(tc, "download_cover",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")) or False)

    tracking = {"douyin_1": {"latest_cover": "https://cdn.example.com/reverted.jpg", "latest_aweme_id": "A1"}}
    res = tc.transcode_all(tracking, manifest, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 0
    assert res["changed"] == 1  # 防御性改写 latest_cover → raw
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")


def test_transcode_download_failure_keeps_cdn(tmp_path, monkeypatch):
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    monkeypatch.setattr(tc, "download_cover", lambda url, dest, timeout=15: False)  # 始终失败

    tracking = {"douyin_1": {"latest_cover": "https://cdn.example.com/1.jpg", "latest_aweme_id": "A1"}}
    res = tc.transcode_all(tracking, {}, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 0
    assert res["changed"] == 0
    assert tracking["douyin_1"]["latest_cover"] == "https://cdn.example.com/1.jpg"  # 保留原 CDN，下轮重试
    assert "douyin_1" not in res["manifest"]


def test_transcode_skips_entries_without_cover():
    tracking = {"douyin_x": {"latest_aweme_id": "A9"}}  # 无 latest_cover（count 退化模式）
    res = tc.transcode_all(tracking, {}, OWNER, REPO, BRANCH, covers_dir="assets/covers")
    assert res["total"] == 0
    assert res["changed"] == 0


def test_transcode_uses_latest_cover_cdn_source(tmp_path, monkeypatch):
    """核心回归（于冬来 案例）：latest_cover 已被改写为 raw，但 latest_cover_cdn 持有真实
    CDN 源时，应从 CDN 重新下载，而非停留在旧图。新作品封面“晚到”时 raw 指针无法溯源，
    必须有持久化 CDN 源才能刷新。"""
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"OLD")  # 旧封面已存在
    calls = []

    def fake(url, dest, timeout=15):
        calls.append(url)
        with open(dest, "wb") as f:
            f.write(b"NEW")
        return True

    monkeypatch.setattr(tc, "download_cover", fake)

    cdn = "https://cdn.example.com/new_arrived.jpg"
    tracking = {"douyin_1": {
        "latest_cover": raw_of(cdir, "douyin_1"),  # 已是 raw（上一轮改写）
        "latest_cover_cdn": cdn,                    # 持久化 CDN 源
        "latest_aweme_id": "A2",
    }}
    res = tc.transcode_all(tracking, {}, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 1
    assert calls == [cdn]  # 必须从 CDN 下载，而非 raw
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")
    assert tracking["douyin_1"]["latest_cover_cdn"] == cdn  # CDN 源保留，下次仍可刷新
    assert res["manifest"]["douyin_1"]["cover_url"] == cdn
    assert res["manifest"]["douyin_1"]["aweme_id"] == "A2"


def test_transcode_cdn_unchanged_no_redownload(tmp_path, monkeypatch):
    """latest_cover_cdn 与 manifest 记录一致 → 不重新下载，仅确保指针为 raw。"""
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"img")
    cdn = "https://cdn.example.com/stable.jpg"
    manifest = {"douyin_1": {"aweme_id": "A1", "sha256": "x", "cover_url": cdn}}

    monkeypatch.setattr(tc, "download_cover",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下载")) or False)

    tracking = {"douyin_1": {
        "latest_cover": raw_of(cdir, "douyin_1"),
        "latest_cover_cdn": cdn,
        "latest_aweme_id": "A1",
    }}
    res = tc.transcode_all(tracking, manifest, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 0
    assert res["changed"] == 0
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")


def test_transcode_cdn_changed_retries_download(tmp_path, monkeypatch):
    """latest_cover_cdn 变化（封面晚到/刷新）→ 重新下载，即便 latest_cover 仍是 raw。"""
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"old")
    old_cdn = "https://cdn.example.com/old.jpg"
    new_cdn = "https://cdn.example.com/new.jpg"
    manifest = {"douyin_1": {"aweme_id": "A1", "sha256": "x", "cover_url": old_cdn}}

    monkeypatch.setattr(tc, "download_cover", lambda url, dest, timeout=15: True)

    tracking = {"douyin_1": {
        "latest_cover": raw_of(cdir, "douyin_1"),
        "latest_cover_cdn": new_cdn,
        "latest_aweme_id": "A1",  # 同一作品，但封面源变了
    }}
    res = tc.transcode_all(tracking, manifest, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 1
    assert res["manifest"]["douyin_1"]["cover_url"] == new_cdn


def test_transcode_cdn_download_failure_keeps_raw(tmp_path, monkeypatch):
    """CDN 源存在但下载失败 → latest_cover 保持 raw（不回退成 cdn），manifest 不写入，下轮重试。"""
    covers = tmp_path / "assets" / "covers"
    cdir = str(covers)
    covers.mkdir(parents=True)
    with open(covers / "douyin_1.jpg", "wb") as f:
        f.write(b"old")
    monkeypatch.setattr(tc, "download_cover", lambda *a, **k: False)

    cdn = "https://cdn.example.com/broken.jpg"
    tracking = {"douyin_1": {
        "latest_cover": raw_of(cdir, "douyin_1"),
        "latest_cover_cdn": cdn,
        "latest_aweme_id": "A2",
    }}
    res = tc.transcode_all(tracking, {}, OWNER, REPO, BRANCH, covers_dir=cdir)
    assert res["downloaded"] == 0
    assert res["changed"] == 0
    assert tracking["douyin_1"]["latest_cover"] == raw_of(cdir, "douyin_1")  # 仍 raw
    assert "douyin_1" not in res["manifest"]


def test_transcode_covers_module_grep_contract():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "transcode_covers.py"), encoding="utf-8").read()
    for token in ["def transcode_all", "def download_cover", "def rewrite_latest_cover",
                  "assets/covers", "urllib.request"]:
        assert token in src, f"transcode_covers.py 缺少契约标记: {token}"
