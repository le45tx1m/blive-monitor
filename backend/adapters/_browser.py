"""浏览器启动统一入口：优先 patchright（浏览器级隐身抗风控），回退 playwright。

patchright 是 Playwright 的反检测 fork，在浏览器**二进制层**抹除自动化指纹
（剔除 ``--enable-automation``、``navigator.webdriver``、CDP 泄漏、Canvas/WebGL
指纹噪音），比单条 ``add_init_script`` 覆盖更强，可降低快手风控的
``result=2`` / ``400002`` 验证码频率。

参考：RSSHub ``lib/utils/playwright.ts`` 同样优先 patchright（公共服务不能要用户
Cookie，其反风控正依赖浏览器级隐身 + 代理轮换）。

若环境未安装 patchright，则回退原生 playwright，保证监控不中断（仅隐身减弱）。
"""
try:
    from patchright.sync_api import sync_playwright
    _BACKEND = "patchright"
except ImportError:  # pragma: no cover - 兜底：未装 patchright 时用原生 playwright
    from playwright.sync_api import sync_playwright
    _BACKEND = "playwright"

__all__ = ["sync_playwright", "browser_backend"]


def browser_backend() -> str:
    """当前实际使用的底层（patchright / playwright），便于日志与排障。"""
    return _BACKEND
