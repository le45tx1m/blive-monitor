"""直播 tab（renderLive）直播间链接可点击性测试（快速模式小特性）。

需求：未开播(offline/replay/error/pending)的房间也应能点进直播间，
之前仅 live 状态才渲染「进入直播间 →」链接，现在放宽为「只要有合法
直播间 URL（u!=='#'）就渲染链接」，非 live 状态文案改为「查看直播间 →」。

测试分两类：
  1) 结构性断言：monitor.html 中 act 不再仅以 st==='live' 为唯一闸门；
  2) 功能性断言：用 node 抽取真实 renderLive() 与 e()，置于最小 DOM/全局
     桩中实跑，构造「live + offline + 未知平台」三类 mock 房间，校验
     liveBody.innerHTML 的链接文案与 href。无 node 时 skip（不报错）。
"""
import json
import os
import re
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_HTML = os.path.join(REPO, "monitor.html")


def _has_node() -> bool:
    try:
        return subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


def _read_monitor() -> str:
    with open(MONITOR_HTML, encoding="utf-8") as f:
        return f.read()


def _extract_js(html: str, sig: str) -> str:
    """从 monitor.html 提取某个顶层多行 function 的完整源码（闭合 `}` 位于行首）。"""
    m = re.search(re.escape(sig) + r"\{.*?\n\}", html, re.S)
    assert m, f"未能从 monitor.html 提取 {sig}"
    return m.group(0)


def _extract_single_line_js(html: str, sig: str) -> str:
    """提取某个单行 function 的完整源码（函数体不含 `}`）。"""
    m = re.search(re.escape(sig) + r"\{[^}]*\}", html)
    assert m, f"未能从 monitor.html 提取单行函数 {sig}"
    return m.group(0)


# ==================== 结构性断言（不依赖 node） ====================

def test_live_room_link_not_gated_only_on_live():
    """回归闸门：act 链接不再仅以 st==='live' 为唯一条件；应放宽为合法 u，
    且同时存在 live 文案「进入直播间 →」与非 live 文案「查看直播间 →」。"""
    html = _read_monitor()
    assert "u !== '#'" in html, "act 链接应放宽为 u !== '#' 闸门（未知平台不渲染）"
    assert "进入直播间" in html, "live 状态仍保留「进入直播间 →」文案"
    assert "查看直播间" in html, "应新增「查看直播间 →」文案供非 live 状态使用"
    # 旧的「仅 live」写法应已被移除
    assert "(s&&st==='live')" not in html, "不应再仅以 st==='live' 为唯一闸门"


# ==================== 功能性断言（node 抽取真实函数实跑） ====================

@pytest.mark.skipif(not _has_node(), reason="node 不可用，跳过前端真实函数校验")
def test_live_offline_unknown_rooms_link_behavior():
    """实跑 monitor.html 内 renderLive()：
      - live 房间(bilibili)  → 「进入直播间 →」，href=https://live.bilibili.com/<id>
      - offline 房间(douyin) → 「查看直播间 →」，href=https://live.douyin.com/<id>
      - 未知平台房间(u==='#') → 不渲染任何 class="act" 链接
    """
    html = _read_monitor()
    e_js = _extract_single_line_js(html, "function e(s)")
    avatar_js = _extract_js(html, "function roomAvatar(r, s)")
    render_js = _extract_js(html, "function renderLive()")

    rooms = [
        {"platform": "bilibili", "id": "123", "name": "Room A"},
        {"platform": "douyin", "id": "456", "name": "Room B"},
        {"platform": "kuaishou", "id": "1011", "name": "Room D"},
        {"platform": "unknown", "id": "789", "name": "Room C"},
    ]
    stat = {
        "updated": "2026-07-10 10:00",
        "rooms": [
            {"platform": "bilibili", "id": "123", "name": "Room A",
             "status": "live", "title": "直播标题", "online": 100},
            {"platform": "douyin", "id": "456", "name": "Room B",
             "status": "offline", "title": "未开播标题"},
            {"platform": "kuaishou", "id": "1011", "name": "Room D",
             "status": "offline", "title": "未开播标题"},
            {"platform": "unknown", "id": "789", "name": "Room C",
             "status": "offline"},
        ],
    }

    harness = (
        "var liveBody = { innerHTML: '' };\n"
        "var document = { getElementById: function(id){"
        " return id === 'liveBody' ? liveBody : { innerHTML: '' }; } };\n"
        "var rooms = %s;\n"
        "var stat = %s;\n"
        "var fl = 'all';\n"
        "var q = '';\n"
        "var hasApi = true;\n"
        "%s\n"   # e()
        "%s\n"   # roomAvatar
        "%s\n"   # renderLive()
        "renderLive();\n"
        "console.log(JSON.stringify(liveBody.innerHTML));\n"
    ) % (json.dumps(rooms), json.dumps(stat), e_js, avatar_js, render_js)

    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        f.write(harness)
    finally:
        f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip())
    finally:
        os.unlink(f.name)

    # monitor-b 以可点击头像（.blm-room-avatar-link）作为进入直播间入口，替代旧底部按钮；
    # live/offline 的视觉区分由状态徽标（blm-live-badge / blm-offline-badge）承担。
    # 可点击性（u !== '#' 闸门）与链接 href 均保持不变。
    # live 房间：头像链接进入直播间，href 为 bilibili 直播间地址；class 为 blm-room-avatar-link
    assert "进入直播间" in out
    assert "https://live.bilibili.com/123" in out
    assert '<a class="blm-room-avatar-link" href="https://live.bilibili.com/123"' in out, (
        "live 房间进入直播间入口 class 应为 blm-room-avatar-link"
    )
    assert "blm-live-badge" in out, (
        "live 房间应通过 blm-live-badge 呈现开播强调（替代旧 act/act-off 区分）"
    )
    # offline 房间：头像同样可点击进入直播间，href 为 douyin 直播间地址；class 为 blm-room-avatar-link
    assert "https://live.douyin.com/456" in out
    assert '<a class="blm-room-avatar-link" href="https://live.douyin.com/456"' in out, (
        "offline 房间进入直播间入口 class 应为 blm-room-avatar-link"
    )
    assert "blm-offline-badge" in out, "offline 房间应通过 blm-offline-badge 呈现未开播状态"
    # 快手房间：头像可点击进入直播间，href 为 live.kuaishou.com/u/<id>；class 含 ava-kuaishou
    assert "https://live.kuaishou.com/u/1011" in out
    assert '<a class="blm-room-avatar-link" href="https://live.kuaishou.com/u/1011"' in out, (
        "快手房间进入直播间入口 href 应为 live.kuaishou.com/u/1011"
    )
    assert "ava-kuaishou" in out, "快手房间头像 class 应为 ava-kuaishou"
    # 未知平台房间（u==='#'）：不应生成任何链接（bili/douyin/kuaishou 各 1 个，未知不渲染）
    assert out.count('class="blm-room-avatar-link"') == 3, (
        "bilibili/douyin/kuaishou 各应渲染 1 个 blm-room-link，未知平台不渲染，实际：%s" % out
    )
    assert 'href="#"' not in out, "未知平台不应渲染直播间链接"


# ==================== 颜色区分：开播/未开播按钮不应同色（结构性断言） ====================

def test_room_link_css_rule_exists():
    """<style> 内必须存在 .blm-room-avatar-link 的 CSS 规则（monitor-b 以可点击头像作为进入入口）。"""
    html = _read_monitor()
    # 抽取 <style ...>...</style> 区块再做断言（<style> 带 id 属性，须容忍属性）
    style = re.search(r"<style[^>]*>.*?</style>", html, re.S)
    assert style, "monitor.html 缺少 <style> 区块"
    style_text = style.group(0)
    assert re.search(r"\.blm-room-avatar-link\s*\{", style_text), (
        "应在 <style> 内定义 .blm-room-avatar-link 链接样式规则（进入直播间入口）"
    )
    # live 房间头像外环高亮复用状态变量（--state-live / --shadow-glow-live），不引入新变量
    assert "var(--state-live)" in style_text, (
        ".blm-room-card.live .blm-room-avatar-link 应使用 --state-live 状态变量高亮"
    )
    # 快手头像配色规则（.ava-kuaishou）必须存在
    assert re.search(r"\.blm-room-avatar\.ava-kuaishou\s*\{", style_text), (
        "应在 <style> 内定义 .blm-room-avatar.ava-kuaishou 快手头像配色规则"
    )


def test_render_live_link_uses_blm_room_avatar_link_class():
    """进入直播间入口由 roomAvatar() 构造：统一使用 .blm-room-avatar-link 可点击头像，
    href 指向直播间地址；title 为「进入直播间」（monitor-b 设计，替代旧底部按钮）。"""
    html = _read_monitor()
    # roomAvatar 内构造头像链接，class 为 blm-room-avatar-link
    assert "blm-room-avatar-link" in html, "roomAvatar 应使用 blm-room-avatar-link 作为进入直播间入口"
    # 头像链接 title 为「进入直播间」
    assert 'title="进入直播间"' in html, "进入直播间头像链接 title 应为「进入直播间」"
    # 头像点击进入直播间：bilibili/douyin 直播间地址拼接逻辑存在
    assert "https://live.bilibili.com/" in html, "bilibili 直播间地址拼接应存在"
    assert "https://live.douyin.com/" in html, "douyin 直播间地址拼接应存在"


def test_room_link_styled_and_live_offline_badge_distinct():
    """回归：进入直播间入口 .blm-room-avatar-link 有定义样式，且 live 房间头像外环高亮
    （开播醒目），live/offline 状态区分通过 .blm-live-badge / .blm-offline-badge 呈现。"""
    html = _read_monitor()
    style = re.search(r"<style[^>]*>.*?</style>", html, re.S).group(0)
    # 头像链接有定义样式
    assert re.search(r"\.blm-room-avatar-link\s*\{", style)
    # live 房间头像外环高亮（开播醒目）
    assert re.search(r"\.blm-room-card\.live \.blm-room-avatar-link", style), (
        "live 房间头像链接应有外环高亮样式"
    )
    # live/offline 状态区分徽标存在
    assert ".blm-live-badge" in html, "应存在 .blm-live-badge 开播徽标（区分 live/offline）"
    assert ".blm-offline-badge" in html, "应存在 .blm-offline-badge 未开播徽标（区分 live/offline）"


@pytest.mark.skipif(not _has_node(), reason="node 不可用，跳过前端真实函数颜色校验")
def test_live_offline_room_classes_real_run():
    """实跑 renderLive()：断言 live 房间链接 class 为 'act'、offline 房间为
    'act act-off'（直接抽取真实函数，无 node 时 skip）。"""
    html = _read_monitor()
    e_js = _extract_single_line_js(html, "function e(s)")
    avatar_js = _extract_js(html, "function roomAvatar(r, s)")
    render_js = _extract_js(html, "function renderLive()")

    rooms = [
        {"platform": "bilibili", "id": "123", "name": "Room A"},
        {"platform": "douyin", "id": "456", "name": "Room B"},
    ]
    stat = {
        "updated": "2026-07-10 10:00",
        "rooms": [
            {"platform": "bilibili", "id": "123", "name": "Room A",
             "status": "live", "title": "直播标题", "online": 100},
            {"platform": "douyin", "id": "456", "name": "Room B",
             "status": "offline", "title": "未开播标题"},
        ],
    }

    harness = (
        "var liveBody = { innerHTML: '' };\n"
        "var document = { getElementById: function(id){"
        " return id === 'liveBody' ? liveBody : { innerHTML: '' }; } };\n"
        "var rooms = %s;\n"
        "var stat = %s;\n"
        "var fl = 'all';\n"
        "var q = '';\n"
        "var hasApi = true;\n"
        "%s\n%s\n%s\n"
        "renderLive();\n"
        "console.log(JSON.stringify(liveBody.innerHTML));\n"
    ) % (json.dumps(rooms), json.dumps(stat), e_js, avatar_js, render_js)

    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        f.write(harness)
    finally:
        f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip())
    finally:
        os.unlink(f.name)

    # monitor-b 以可点击头像作为进入入口，live / offline 房间统一使用 .blm-room-avatar-link
    assert '<a class="blm-room-avatar-link" href="https://live.bilibili.com/123"' in out
    assert '<a class="blm-room-avatar-link" href="https://live.douyin.com/456"' in out
    assert out.count('class="blm-room-avatar-link"') == 2
