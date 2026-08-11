#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快手抓取通道可行性探针（一次性诊断工具，非业务代码）。

目的
----
本地沙箱（数据中心 IP）实测：快手四条通道全部被挡——
  * graphql visionProfilePhotoList  -> result=2 / 50（且前端已弃用该端点）
  * www.kuaishou.com /rest/v/profile/feed -> result=109 需登录
  * v.m.chenzhongtech.com /rest/wd/feed/profile -> result=2001 需滑块验证码

而抖音在同一 IP 上免 Cookie 可抓。差异是否来自「出口 IP 段」尚未证实。
GitHub Actions runner 与本沙箱出口 IP 不同，故必须在 runner 上复测，
再决定「照抖音逻辑」的浏览器拦截方案在 CI 上是否成立。

产出
----
把结构化结论写入 docs/kuaishou_probe_result.json，由 workflow 提交回仓库。

用法
----
    python3 tools/kuaishou_probe.py [principal_id]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_PID = "3xrgxqkqp829xz6"  # Sandy88888
OUT_PATH = os.path.join("docs", "kuaishou_probe_result.json")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SHARE_HOST = "https://v.m.chenzhongtech.com"
FEED_PATH = "/rest/wd/feed/profile"


def _classify(body: str) -> str:
    """把响应体归类成人类可读的结论。"""
    if not body:
        return "empty"
    for code, name in (
        ('"result":2001', "BLOCKED_CAPTCHA"),
        ('"result":109', "NEED_LOGIN"),
        ('"result":2,', "BLOCKED_ANTISPAM"),
        ('"result":50', "EMPTY_RESULT_50"),
    ):
        if code in body:
            return name
    if '"result":1' in body and ("feeds" in body or "list" in body):
        return "OK"
    return "UNKNOWN"


def probe_egress_ip() -> dict:
    """记录 runner 出口 IP —— 判定风控是否 IP 段相关的关键证据。"""
    info = {}
    for url, key in (
        ("https://api.ipify.org?format=json", "ip"),
        ("https://ipinfo.io/json", "detail"),
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DESKTOP_UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                info[key] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            info[key] = {"error": str(exc)}
    return info


def probe_raw_share_api(pid: str) -> dict:
    """裸 HTTP 直打分享域作品列表接口（无浏览器、无签名）。"""
    url = f"{SHARE_HOST}{FEED_PATH}?kpn=KUAISHOU&captchaToken="
    payload = json.dumps({"eid": pid, "count": 9, "pcursor": ""}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": MOBILE_UA,
            "Referer": f"{SHARE_HOST}/fw/user/{pid}",
            "Origin": SHARE_HOST,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        return {"status": r.status, "verdict": _classify(body), "body": body[:600]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "verdict": "ERROR"}


def probe_browser(pid: str) -> dict:
    """
    抖音式：真浏览器打开移动分享页，拦截页面自身发出的作品列表 XHR。
    这正是 check_new_posts.get_latest_aweme 对抖音所做的事。
    """
    try:
        from backend.adapters._browser import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return {"error": f"playwright unavailable: {exc}", "verdict": "ERROR"}

    result: dict = {"intercepted": [], "page_redirected_to_captcha": None}
    exe = os.environ.get("CHROME_PATH") or None

    with sync_playwright() as p:
        launch_kw = {
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        }
        if exe:
            launch_kw["executable_path"] = exe
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=3,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.new_page()

        def on_response(resp):
            if FEED_PATH not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                return
            result["intercepted"].append(
                {"status": resp.status, "verdict": _classify(body), "body": body[:600]}
            )

        page.on("response", on_response)
        try:
            page.goto(
                f"{SHARE_HOST}/fw/user/{pid}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(8000)
        except Exception as exc:  # noqa: BLE001
            result["goto_error"] = str(exc)

        result["final_url"] = page.url[:200]
        result["page_redirected_to_captcha"] = "captcha" in page.url
        browser.close()

    verdicts = [i["verdict"] for i in result["intercepted"]]
    if "OK" in verdicts:
        result["verdict"] = "OK"
    elif verdicts:
        result["verdict"] = verdicts[0]
    else:
        result["verdict"] = "NO_XHR_CAPTURED"
    return result


def probe_live_api_feed(pid: str) -> dict:
    """复刻生产路径：直接用真实 ``KuaishouFeedSession`` 跑 ``live_api/profile/public``。

    与旧两条通道（裸分享域接口 / 移动端浏览器拦截）不同，这条才是 check_new_posts.py
    线上实际在用的。探针直接 import 真实适配器类，测的就是线上跑的代码，避免探针与
    业务代码漂移；输出的 ``seen``（响应状态码序列）能直接看出预热是否种下 token、
    以及是否卡在纯 ``result=2``（IP 信誉问题）还是会进展到 ``1``。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from backend.adapters.kuaishou_feed import (  # noqa: E402
            KuaishouFeedSession, ANTIBOT_COOKIES,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import kuaishou_feed failed: {exc}", "verdict": "ERROR"}

    try:
        from backend.adapters._browser import sync_playwright  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        return {"error": f"playwright unavailable: {exc}", "verdict": "ERROR"}

    exe = os.environ.get("CHROME_PATH") or None
    out: dict = {}
    with sync_playwright() as p:
        launch_kw = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        }
        if exe:
            launch_kw["executable_path"] = exe
        browser = p.chromium.launch(**launch_kw)
        main_ctx = browser.new_context(
            user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 900}, locale="zh-CN")
        sess = KuaishouFeedSession(main_ctx, user_agent=DESKTOP_UA)
        try:
            r = sess.fetch(pid)
            out = {
                "ok": r.get("ok"),
                "result": r.get("result"),
                "nav_count": r.get("nav_count"),
                "seen": r.get("seen"),
                "items": len(r.get("items") or []),
                "author_name": r.get("author_name"),
                "detail": (r.get("detail") or "")[:300],
            }
        except Exception as exc:  # noqa: BLE001
            out = {"error": str(exc), "verdict": "ERROR"}
        sess.close()
        main_ctx.close()
        browser.close()

    out["verdict"] = "OK" if out.get("ok") else "NO_RESULT1"
    out["antibot_cookies_expected"] = list(ANTIBOT_COOKIES)
    return out


def main() -> int:
    pid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PID
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner": os.environ.get("RUNNER_ENV") or ("github-actions" if os.environ.get("CI") else "local"),
        "principal_id": pid,
        "egress": probe_egress_ip(),
        "channels": {},
    }

    print("== 1/3 裸 HTTP 分享域接口 ==", flush=True)
    report["channels"]["raw_share_api"] = probe_raw_share_api(pid)
    print(json.dumps(report["channels"]["raw_share_api"], ensure_ascii=False)[:400], flush=True)

    print("== 2/3 浏览器拦截（抖音式/移动端）==", flush=True)
    report["channels"]["browser_intercept"] = probe_browser(pid)
    print(json.dumps(report["channels"]["browser_intercept"], ensure_ascii=False)[:800], flush=True)

    print("== 3/3 生产路径 live_api/profile/public（真实适配器类）==", flush=True)
    report["channels"]["live_api_profile_public"] = probe_live_api_feed(pid)
    print(json.dumps(report["channels"]["live_api_profile_public"], ensure_ascii=False)[:800], flush=True)

    verdicts = {k: v.get("verdict") for k, v in report["channels"].items()}
    report["summary"] = verdicts
    report["feasible_without_credentials"] = "OK" in verdicts.values()

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\n== SUMMARY ==", flush=True)
    print(json.dumps(verdicts, ensure_ascii=False, indent=2), flush=True)
    print(f"feasible_without_credentials = {report['feasible_without_credentials']}", flush=True)
    print(f"written -> {OUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
