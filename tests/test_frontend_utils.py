"""前端关键纯函数测试。

从 monitor.html 抽取 JS 函数定义，用 node 子进程实跑，断言安全关键行为。
重点：e() 必须转义单双引号（防 XSS）。

node 不可用时整体 skip。
"""

import json
import os
import re
import subprocess
import tempfile

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


def _extract_fn(html: str, fn_name: str) -> str:
    """从 HTML 中提取完整的 `function fn_name(...){...}` 定义。

    用大括号深度匹配（不依赖固定行号），返回从 `function` 到匹配 `}` 的完整文本。
    """
    pat = r"function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)\s*\{"
    m = re.search(pat, html)
    assert m, f"未找到函数 {fn_name}"

    # 从 { 开始做大括号深度匹配
    brace_start = m.end() - 1  # 指向 '{'
    depth = 0
    i = brace_start
    while i < len(html):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                # 返回从 function 关键字到 } 的完整定义
                return html[m.start() : i + 1]
        i += 1
    raise AssertionError(f"函数 {fn_name} 大括号不平衡")


def _run_node(js_code: str, expr: str):
    """执行 js_code + `; console.log(JSON.stringify(<expr>))`，返回解析后的 JSON。"""
    full = js_code + f"\nconsole.log(JSON.stringify({expr}));"
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        f.write(full)
    finally:
        f.close()
    try:
        r = subprocess.run(["node", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if r.returncode != 0:
        raise RuntimeError(f"node 执行失败: {r.stderr}")
    return json.loads(r.stdout.strip())


# ==================== e() HTML 转义函数 ====================

@pytest.mark.skipif(not _has_node(), reason="node 不可用")
class TestEscapeFunction:
    """e() 必须转义 & < > " '，防止 XSS。"""

    def _get_e(self):
        html = _read_monitor()
        return _extract_fn(html, "e")

    def test_escapes_double_quote(self):
        js = self._get_e()
        result = _run_node(js, 'e(\'a"b\')')
        assert "&quot;" in result, "e() 必须转义双引号"

    def test_escapes_single_quote(self):
        js = self._get_e()
        result = _run_node(js, "e(\"a'b\")")
        assert "&#39;" in result, "e() 必须转义单引号"

    def test_escapes_angle_brackets_and_amp(self):
        js = self._get_e()
        result = _run_node(js, 'e("<script>&</script>")')
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result

    def test_null_returns_empty(self):
        js = self._get_e()
        assert _run_node(js, "e(null)") == ""
        assert _run_node(js, "e(undefined)") == ""

    def test_plain_text_unchanged(self):
        js = self._get_e()
        assert _run_node(js, 'e("hello world")') == "hello world"

    def test_non_string_coerced(self):
        js = self._get_e()
        assert _run_node(js, "e(123)") == "123"

    def test_double_escape_predictable(self):
        """二次转义可预测：& 先被转义为 &amp;，再次转义 &amp; -> &amp;amp;"""
        js = self._get_e()
        once = _run_node(js, 'e("&")')
        assert once == "&amp;"
        twice = _run_node(js, 'e(e("&"))')
        assert twice == "&amp;amp;"


# ==================== 结构性断言（不依赖 node） ====================

def test_escape_function_exists():
    html = _read_monitor()
    assert re.search(r"function\s+e\s*\(", html), "monitor.html 缺少 e() 函数"


def test_escape_function_has_quote_escaping():
    """e() 源码必须包含引号转义正则。"""
    html = _read_monitor()
    # 找到 e 函数定义
    m = re.search(r"function\s+e\s*\([^)]*\)\s*\{", html)
    assert m
    # 提取函数体（简单截取后面 500 字符）
    snippet = html[m.start() : m.start() + 500]
    assert "&quot;" in snippet or '\\"' in snippet, "e() 应转义双引号"
    assert "&#39;" in snippet or "\\'" in snippet, "e() 应转义单引号"


def test_escape_html_function_has_single_quote():
    """escapeHtml 也应转义单引号。"""
    html = _read_monitor()
    m = re.search(r"function\s+escapeHtml\s*\([^)]*\)\s*\{", html)
    if m:
        snippet = html[m.start() : m.start() + 500]
        assert "&#39;" in snippet or "'" in snippet, "escapeHtml 应转义单引号"


# ==================== parseBeijing 跨时区不变量 ====================

@pytest.mark.skipif(not _has_node(), reason="node 不可用")
def test_parse_beijing_no_tz_bug():
    """parseBeijing 在不同时区下应返回相同 UTC 毫秒。"""
    html = _read_monitor()
    js = _extract_fn(html, "parseBeijing")

    zones = ["Asia/Shanghai", "UTC", "America/New_York", "Europe/London"]
    results = []
    for z in zones:
        env = dict(os.environ)
        env["TZ"] = z
        full = js + '\nconsole.log(JSON.stringify(parseBeijing("2026-07-10 21:30:52")));'
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        try:
            f.write(full)
        finally:
            f.close()
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True, env=env)
        finally:
            os.unlink(f.name)
        assert r.returncode == 0, f"node 在 TZ={z} 失败: {r.stderr}"
        results.append(json.loads(r.stdout.strip()))

    # 所有时区结果应一致（北京时间 21:30:52 -> UTC 13:30:52）
    assert all(r == results[0] for r in results), f"跨时区结果不一致: {results}"
    expected = 1783690252000  # Date.UTC(2026,6,10,13,30,52)
    assert results[0] == expected, f"parseBeijing 返回值错误: {results[0]} != {expected}"
