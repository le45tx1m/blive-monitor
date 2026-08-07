"""阶段三 T05：快手适配器（直播 + 新作，优雅降级）。

直播主路径为 SSR 解析（live.kuaishou.com/u/<id> 的 window.__INITIAL_STATE__），
真实结构：liveroom.playList[0].isLiving / .liveStream.*；旧结构 liveroom.living 兼容。
新作：visionProfilePhotoList graphql 返回 {"result":2}（风控/未登录）→ raise AdapterGated。
"""

import urllib.error
import urllib.request

import pytest

from backend.adapters import AdapterGated
from backend.adapters.kuaishou import KuaishouAdapter


class _FakeResp:
    """供 monkeypatch urllib.request.urlopen 的伪响应（支持 with 上下文）。"""

    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> bool:
        return False


def test_kuaishou_capability_flags():
    a = KuaishouAdapter()
    assert a.platform == "kuaishou"
    assert a.supports_live is True
    assert a.supports_posts is True
    assert a.needs_context is False
    assert a.poll_interval == 300


# ---------------- 直播：SSR 主路径 ----------------

def _ssr_html(liveroom_state: str) -> bytes:
    return (
        '<html><body><script>window.__INITIAL_STATE__='
        + liveroom_state
        + ';</script></body></html>'
    ).encode("utf-8")


def test_kuaishou_live_ssr_real_structure(monkeypatch):
    """真实 SSR 结构：playList[0].isLiving=true + liveStream.*。"""
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":true,'
        '"liveStream":{"caption":"直播中标题","watcherCount":123,"coverUrl":"http://c.jpg"},'
        '"author":{}}]}}'
    )

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    m = KuaishouAdapter().fetch_room_status("123")
    assert m.live_status is True
    assert m.title == "直播中标题"
    assert m.online == 123
    assert m.cover == "http://c.jpg"
    assert m.extra.get("source") == "ssr"


def test_kuaishou_live_ssr_offline(monkeypatch):
    """playList[0].isLiving=false → offline（非直播态）。"""
    html = _ssr_html('{"liveroom":{"playList":[{"isLiving":false,"liveStream":{}}]}}')

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    m = KuaishouAdapter().fetch_room_status("123")
    assert m.live_status is False
    assert m.extra.get("source") == "ssr"


def test_kuaishou_live_degrade_on_failure(monkeypatch):
    def fake_raise(self, *args, **kwargs):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_raise)
    m = KuaishouAdapter().fetch_room_status("123")
    # SSR 解析失败 -> 优雅降级为 offline，不抛异常
    assert m.live_status is False
    assert m.extra.get("degraded") is True


def test_kuaishou_room_from_html_ssr():
    """兼容旧结构 liveroom.living（无 playList）。"""
    html = (
        'var x=1;window.__INITIAL_STATE__={"liveroom":'
        '{"living":true,"caption":"SSR标题","watcherCount":42,"coverUrl":"http://s.jpg"}};'
        "</script>"
    )
    m = KuaishouAdapter._room_from_html("123", html)
    assert m.live_status is True
    assert m.title == "SSR标题"
    assert m.online == 42
    assert m.extra.get("source") == "ssr"


def test_kuaishou_room_from_html_real_playlist():
    """真实结构直接从 _room_from_html 解析。"""
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":true,'
        '"liveStream":{"caption":"真实标题","watcherCount":7,"coverUrl":"http://r.jpg"}}]}}'
    )
    m = KuaishouAdapter._room_from_html("123", html)
    assert m.live_status is True
    assert m.title == "真实标题"
    assert m.online == 7
    assert m.cover == "http://r.jpg"


def test_kuaishou_room_from_html_no_state():
    """无 __INITIAL_STATE__ → offline（不抛异常）。"""
    m = KuaishouAdapter._room_from_html("123", "<html>no state</html>")
    assert m.live_status is False


# ---------------- 新作：graphql ----------------

def test_kuaishou_new_posts_success(monkeypatch):
    def fake_photos(self, rid):
        return [
            {
                "photoId": "p1",
                "caption": "c1",
                "coverUrl": "http://c",
                "url": "http://u",
                "timestamp": 1000,
                "is_image": False,
            }
        ]

    monkeypatch.setattr(KuaishouAdapter, "_fetch_graphql_photos", fake_photos)
    posts = KuaishouAdapter().fetch_new_posts("rid", baseline={})
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "p1"
    assert p.extra.get("conf") == "api"
    assert p.extra.get("type") == "视频"
    assert p.extra.get("dedup_key") == "post:kuaishou:p1"


def test_kuaishou_new_posts_baseline_filter(monkeypatch):
    def fake_photos(self, rid):
        return [{"photoId": "p1", "caption": "c1", "timestamp": 1000, "is_image": False}]

    monkeypatch.setattr(KuaishouAdapter, "_fetch_graphql_photos", fake_photos)
    # 基线已含 p1 -> 视为无新作
    posts = KuaishouAdapter().fetch_new_posts("rid", baseline={"latest_post_id": "p1"})
    assert posts == []


def test_kuaishou_new_posts_gated_on_failure(monkeypatch):
    def fake_photos(self, rid):
        raise RuntimeError("风控")

    monkeypatch.setattr(KuaishouAdapter, "_fetch_graphql_photos", fake_photos)
    with pytest.raises(AdapterGated):
        KuaishouAdapter().fetch_new_posts("rid", baseline={})


def test_kuaishou_graphql_result2_raises_gated(monkeypatch):
    """graphql 返回 {"result":2}（风控/未登录）→ _fetch_graphql_photos raise AdapterGated。"""
    def fake_urlopen(req, timeout=10):
        return _FakeResp(b'{"result":2,"error_msg":null,"request_id":"x"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AdapterGated):
        KuaishouAdapter()._fetch_graphql_photos("rid")


def test_kuaishou_graphql_ok_returns_feeds(monkeypatch):
    """正常 graphql 响应（result 缺失/0）解析为 feeds。"""
    def fake_urlopen(req, timeout=10):
        return _FakeResp(
            b'{"result":0,"data":{"visionProfilePhotoList":{'
            b'"feeds":[{"photoId":"p9","caption":"hi","timestamp":5}]}}}'
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    feeds = KuaishouAdapter()._fetch_graphql_photos("rid")
    assert isinstance(feeds, list) and len(feeds) == 1
    assert feeds[0]["photoId"] == "p9"
