"""前端日志面板结构性测试：monitor.html 含功能化元素，三兄弟为重定向壳。"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


def test_monitor_has_stats_and_filter():
    html = _read("monitor.html")
    assert 'id="logStats"' in html
    assert 'id="logFilter"' in html
    assert 'id="logAccount"' in html
    assert "onLoadMore" in html
    assert "computeStatsJS" in html
    assert "applyFilters" in html
    assert "toggleExpand" in html
    assert "readViewParam" in html


def test_monitor_no_hardcoded_80_truncation():
    html = _read("monitor.html")
    # 旧实现硬编码 hist.length-80 / -60 截断应已移除
    assert "hist.length-80" not in html
    assert "hist.length-60" not in html
    # 分页步长 50 由 logState.visible 控制
    assert "visible" in html


def test_monitor_supports_view_param():
    html = _read("monitor.html")
    assert "view=dashboard" in html
    assert "view=feed" in html
    assert "view=hero" in html


def test_no_legacy_brother_frontends():
    """转正后只保留 monitor.html 一个正式前端；其余变体（dashboard/feed/hero 等）已删除。

    原 test_brothers_are_redirect_shells 守卫 monitor-dashboard/feed/hero 三个重定向壳；
    本次"前端转正 + 删除其余前端"后这些壳文件已不存在，反向守卫：不得重新出现，
    避免历史多前端分裂回归。
    """
    for name in [
        "monitor-dashboard.html",
        "monitor-feed.html",
        "monitor-hero.html",
        "monitor-a.html",
        "monitor-b.html",
        "strata.html",
    ]:
        assert not os.path.exists(os.path.join(REPO, name)), \
            f"{name} 应已删除（只保留 monitor.html 一个正式前端）"
