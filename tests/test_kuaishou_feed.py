"""快手作品流解析（backend/adapters/kuaishou_feed.py）的纯函数测试。

样本全部截自真实响应（账号 pineapple2005 / principalId 3x7ju263tgi5dn9，
以及 Sandy88888 / 3xrgxqkqp829xz6）。**CDN URL 必须用真的**：发布时间就编码在
文件名里，随手编的 URL 解不出时间，等于没测到核心逻辑。
"""

from backend.adapters import kuaishou_feed as kf

# 真实 URL 样本 -------------------------------------------------------------
# 视频：/upic/<日期路径>/B<base64>_b_B<hash>.mp4
#       base64 解出 "20260807162057_180534002_204769936242_1_3"
_REAL_VIDEO_URL = (
    "https://hwmov.a.yximgs.com/upic/2026/08/07/16/"
    "BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_b_"
    "B63e35bd16b353ea0e00535842fce5dbf.mp4?clientCacheKey=3x2ywf5zitae5zg.mp4"
)
# 图集封面：/upic/<日期路径>/B<base64>_B<hash>.jpg（注意没有 _b_ 中缀）
_REAL_POSTER_URL = (
    "https://p2.a.yximgs.com/upic/2025/11/05/08/"
    "BMjAyNTExMDUwODUzNTBfMTgwNTM0MDAyXzE3OTA5MzU1NjQ1NV8xXzY=_"
    "Bbd34b6b79569510e180d2181ef37e6c0.jpg?clientCacheKey=3x65q35quat5aku.jpg"
)
# 另一个账号（Sandy88888），userId 不同 —— 用于归属校验
_REAL_SANDY_URL = (
    "https://p2.a.yximgs.com/upic/2021/12/22/21/"
    "BMjAyMTEyMjIyMTUzNDVfMjExNzU1MF82MzMxMzA5MDY2MF8xXzM=_"
    "B32b4c89222a98e61e7da56e53c6e10e5.jpg?clientCacheKey=3x6zh63ab9abu9g.jpg"
)


# ---------------- 时间/归属反解 ----------------

def test_从视频URL反解发布时间和作者():
    ts, uid = kf.decode_media_meta(_REAL_VIDEO_URL)
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2026-08-07 16:20:57"
    assert uid == "180534002"


def test_从封面URL反解发布时间和作者():
    ts, uid = kf.decode_media_meta(_REAL_POSTER_URL)
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2025-11-05 08:53:50"
    assert uid == "180534002"


def test_不同账号反解出不同userId():
    _, uid = kf.decode_media_meta(_REAL_SANDY_URL)
    assert uid == "2117550"


def test_反解时间不随系统时区漂移(monkeypatch):
    """CDN 文件名里是北京时间，换算 epoch 必须显式按 +8，不能跟随 runner 时区。

    GitHub Actions runner 默认 UTC，用裸 datetime.timestamp() 会整体偏 8 小时 ——
    这个坑本项目在 kuaishou._ts_to_bj 上已经踩过一次。
    """
    import time

    from common import epoch_to_beijing

    for tz in ("UTC", "America/New_York", "Asia/Shanghai"):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        try:
            ts, _ = kf.decode_media_meta(_REAL_VIDEO_URL)
            assert epoch_to_beijing(ts) == "2026-08-07 16:20:57", tz
        finally:
            monkeypatch.undo()
            time.tzset()


def test_无法反解时返回空():
    assert kf.decode_media_meta("https://example.com/a.jpg") == (None, "")
    assert kf.decode_media_meta("") == (None, "")
    assert kf.decode_media_meta(None) == (None, "")


def test_路径日期作为降级():
    """base64 段缺失时，退到路径里的日期（精确到小时，好过没有）。"""
    ts, uid = kf.decode_media_meta("https://p2.a.yximgs.com/upic/2026/08/07/16/plain.jpg")
    from common import epoch_to_beijing
    assert epoch_to_beijing(ts) == "2026-08-07 16:00:00"
    assert uid == ""


# ---------------- 响应解析 ----------------

def _payload(items, result=1, living=False):
    return {"data": {"list": items, "result": result,
                     "live": {"author": {"living": living}}}}


_ITEMS = [
    {"id": "top1", "poster": _REAL_POSTER_URL, "workType": "multiple", "playUrl": "",
     "imgUrls": ["http://x/a.webp"], "author": {"id": "pineapple2005", "name": "魅力驿站"}},
    {"id": "new1", "poster": _REAL_VIDEO_URL, "workType": "video",
     "playUrl": _REAL_VIDEO_URL, "author": {"id": "pineapple2005", "name": "魅力驿站"}},
]


def test_解析真实响应():
    got = kf.parse_profile_public(_payload(_ITEMS))
    assert got["ok"] is True
    assert got["result"] == 1
    assert len(got["items"]) == 2
    assert got["author_name"] == "魅力驿站"
    assert got["author_id"] == "pineapple2005"
    assert got["living"] is False


def test_result2视为未拿到():
    """匿名被挡（预热不足），不是「没有新作品」。"""
    got = kf.parse_profile_public(_payload([], result=2))
    assert got["ok"] is False
    assert got["result"] == 2


def test_空列表即使result1也不算成功():
    assert kf.parse_profile_public(_payload([], result=1))["ok"] is False


def test_解析垃圾输入不抛异常():
    for bad in (None, "", 123, {}, {"data": "x"}, {"data": {"list": "x"}}):
        got = kf.parse_profile_public(bad)
        assert got["ok"] is False


def test_图文与视频类型判定():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    by_id = {i["photo_id"]: i for i in items}
    assert by_id["top1"]["is_image"] is True     # workType=multiple
    assert by_id["new1"]["is_image"] is False    # workType=video


def test_缺id的脏条目被丢弃():
    items = kf.parse_profile_public(_payload([{"poster": _REAL_VIDEO_URL}]))["items"]
    assert items == []


# ---------------- 排序与取最新 ----------------

def test_按时间排序而非列表顺序():
    """列表首位是 2025-11-05 的置顶，真正最新是 2026-08-07。"""
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    assert [i["photo_id"] for i in kf.sort_by_time(items)] == ["new1", "top1"]
    assert kf.pick_latest(items)["photo_id"] == "new1"


def test_无时间的条目排最后且不冒充最新():
    items = kf.parse_profile_public(_payload([
        {"id": "unknown", "poster": "https://example.com/x.jpg", "workType": "video",
         "playUrl": "", "author": {"id": "a", "name": "n"}},
    ]))["items"]
    assert kf.pick_latest(items) is None   # 宁可返回 None 也不猜


def test_空列表取最新返回None():
    assert kf.pick_latest([]) is None


# ---------------- 归属校验 ----------------

def test_归属校验通过():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    ok, why = kf.verify_ownership(items, expect_user_id="180534002",
                                  expect_author_id="pineapple2005")
    assert ok is True and why == ""


def test_归属校验拦截userId不符():
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    ok, why = kf.verify_ownership(items, expect_user_id="2117550")
    assert ok is False and "userId 不匹配" in why


def test_归属校验拦截混入他人作品():
    """同一页出现两个 userId，说明抓到的不全是目标账号的作品 —— 必须整批拒绝。

    本项目在抖音上踩过「随机抓到推荐流」的坑，快手这里从数据层面就能自证。
    """
    mixed = _ITEMS + [{"id": "other", "poster": _REAL_SANDY_URL, "workType": "video",
                       "playUrl": "", "author": {"id": "Sandy88888", "name": "肥阿肥"}}]
    items = kf.parse_profile_public(_payload(mixed))["items"]
    ok, why = kf.verify_ownership(items)
    assert ok is False
    assert "userId 不唯一" in why


def test_归属校验拒绝空列表():
    assert kf.verify_ownership([])[0] is False


def test_无期望值时仅校验内部一致性():
    """首轮还没有基线，只要这批数据自洽就放行（之后自举出强校验）。"""
    items = kf.parse_profile_public(_payload(_ITEMS))["items"]
    assert kf.verify_ownership(items)[0] is True


# ---------------- 文案与链接 ----------------

def test_文案剥离快手后缀():
    assert kf.clean_caption("#热辣一夏-快手") == "#热辣一夏"
    assert kf.clean_caption("某作品 - 快手") == "某作品"


def test_纯站名标题视为无文案():
    assert kf.clean_caption("快手") == ""
    assert kf.clean_caption("快手直播") == ""
    assert kf.clean_caption(None) == ""


def test_作品链接格式():
    assert kf.photo_url("3x2ywf5zitae5zg") == \
        "https://www.kuaishou.com/short-video/3x2ywf5zitae5zg"
