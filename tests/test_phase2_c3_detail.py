"""阶段二 2c C3 · 单房间详情弹层：grep 契约 + 不破坏 show() 5-tab 契约断言。

C3 采用 .blm-modal overlay（仿 roomEdit），与 show() 的 views 字典(5-key) 和
tabs 数组索引完全解耦，零回归风险。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "monitor.html")


def _src():
    return open(HTML, encoding="utf-8").read()


def test_c3_grep_contracts():
    src = _src()
    for token in ["function openRoomDetail",
                  "function renderRoomDetail",
                  "function closeRoomDetail",
                  "id=\"roomDetail\"",
                  "blm-modal"]:
        assert token in src, "monitor.html 缺少 C3 契约标记: %s" % token


def test_c3_modal_is_blm_modal_overlay():
    src = _src()
    # roomDetail 必须是 .blm-modal overlay（与 roomEdit 同构），且 mask/关闭按钮调用 closeRoomDetail
    assert 'id="roomDetail" class="blm-modal"' in src, "roomDetail 应为 .blm-modal"
    assert 'class="blm-modal-mask" onclick="closeRoomDetail()"' in src, "mask 点击应关闭弹层"
    assert 'class="blm-modal-close" title="关闭" onclick="closeRoomDetail()"' in src, "关闭按钮应调用 closeRoomDetail"


def test_c3_esc_closes_modal():
    src = _src()
    assert "ev.key === 'Escape'" in src, "应支持 Esc 关闭弹层"
    assert "closeRoomDetail()" in src, "Esc 处理应调用 closeRoomDetail"


def test_c3_detail_button_wired():
    src = _src()
    # 直播卡与抖音号卡均有「详情」按钮，调用 openRoomDetail
    assert "blm-room-detail" in src, "房间卡应有详情按钮样式类"
    assert 'openRoomDetail(' in src, "应有 openRoomDetail 调用"


def test_c3_show_five_tab_contract_preserved():
    """C3 弹层不得改动 show() 的 5-tab 视图契约。

    show() 必须以 5 个 view（live/posts/log/config/dashboard）为唯一索引集合，
    通过 Object.keys(views) 动态切换 active；C3 弹层用 .blm-modal overlay 承载，
    不得新增第 6 个 view（view-detail）。
    """
    src = _src()
    # views 字典必须仍是这 5 个 key
    assert "var views={'live':'view-live','posts':'view-posts','log':'view-log','config':'view-config','dashboard':'view-dashboard'}" in src, \
        "show() 的 views 字典 5-key 契约被破坏"
    # 切换逻辑应基于 views 的 key 动态遍历（不再依赖硬编码 tabs 数组字面量）
    assert "Object.keys(views).forEach" in src, "show() 应基于 Object.keys(views) 遍历 5 个 view"
    # 主 tab 高亮走 .blm-tab-item[data-key]，key 集合应覆盖 live/posts/log/config
    # （dashboard 按设计不在主 tab 里，由 view-posts/view-dashboard 全屏覆盖承载）
    for k in ["live", "posts", "log", "config"]:
        assert ('data-key="%s"' % k in src) or ("data-key='%s'" % k in src), \
            "show() 主 tab 应含 data-key=%s" % k
    # 不得出现第 6 个 view（C3 弹层不新增 view）
    assert "view-detail" not in src, "不应新增第 6 个 view（破坏 5-tab 契约）"
