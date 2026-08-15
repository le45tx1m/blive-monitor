# 给新 Agent 的提示词（可直接粘贴）

> 使用说明：把下面「提示词 1」整段粘贴给新 agent 作为总任务；进入对应阶段时，再追加「提示词 2/3/4」引导其在该阶段的具体动作。新 agent **直连 GitHub、无需代理**，可 `gh` / 直接 `git push` / `gh workflow run` / `gh run view --log` / `gh api`。

---

## 提示词 1 —— 总任务（角色 + 背景 + 目标）

```
你是资深 Python 爬虫 / 后端工程师，熟悉 Playwright 无头 Chromium、网站风控（visitor JS、设备指纹、anti-bot token）与 GitHub Actions CI。

接手仓库 racheko-lab/blive-monitor（公开 GitHub Pages，分支 master），这是一个用 GitHub Actions + Pages 监测 B站/抖音/快手直播间与新作品的项目。你可以直连 GitHub：gh CLI 可用、git 直连 github.com、GitHub REST API 可读写、可 gh workflow run 触发 CI、可 gh run view --log 看日志。不要假设任何"API 被墙 / 不能触发 CI"的限制——那只是上一个 agent 的沙箱问题，你这里没有。

【背景：已完成的工作】
- 快手游客身份 did 跨运行稳定复用已修好（commit 5f02695f：kuaishou_feed.py 的 _cycle 末尾兜底捕获 did；19a4f1ef：CI web_ 前缀防回退）。验证过 did 从 08-14 13:52Z 起零漂移、gated 不涨。
- 前端 monitor.html 右下角有版本角标、index.html 页脚有版本行，CI 部署时自动用 git 短 SHA + UTC 时间烙印（v<SHA> · <时间>），无需手动 bump。

【当前问题（你的核心任务）】
快手"作品接口"（新作品监控，代码在 backend/adapters/kuaishou_feed.py，由 check_new_posts.py 调用）从 08-14 23:19 UTC 起持续约 20 小时被风控，返回 result=2（"风控预热未通过"），单轮重试 14 次全挂，响应序列 [2,2,...,2]。线上 monitor.html 的"通知健康"面板可见这些告警，受影响 kuaishou 账号：wikw-0403-、HUizi-G-H、wadaxiwachangmingran（早前还有 Sandy88888、ii050824）。
关键：同一时段 kuaishou_guest_visitor.json 里的 did 完全稳定（web_d6d698fea12a0722d654539cc323b3f5 未变），所以 did 复用没坏。问题在风控 token 层——5f02695f 故意不跨运行持久化 kwfv1/kwssectoken/kwscode（次数/时效受限），而 result=2 预热未通过大概率就是这些 token 没拿到/没带上。
注意：post_tracking.json 里的 gated_count / did_used 来自另一条路径（kuaishou.py 直播状态监控），与本次作品接口 gating 无关，别被误导。

【你的目标】
1) 定位 result=2（预热未通过）的真正根因，要基于 CI 日志 + 代码实证，不要猜。
2) 修复，使 kuaishou 作品接口不再持续 result=2（允许偶发，但应能"重试下一轮即可"自愈，而非 14 连挂）。
3) 保持既有 did 跨运行复用不被破坏；保持"免登录、无用户 cookie"的匿名设计（用户硬约束）。
4) 单测通过，必要时补测试；最后给一份简短结论（根因+修复+验证）。

先花 10 分钟读 backend/adapters/kuaishou_feed.py（重点 _warmup / _wait_visitor_cookies / _cycle / _capture_visitor_cookies / _apply_visitor_cookies）和 .github/workflows/check.yml，再用 gh 拉几段近期 CI 日志实证，然后给出你的诊断假设与验证计划，再动手。每步都用中文简要报告发现。
```

---

## 提示词 2 —— 进入诊断阶段（定向实证）

```
进入诊断。请按以下顺序用你的直连能力实证，不要空想：

1) gh run list --repo racheko-lab/blive-monitor --limit 15  找 result=2 出现的 run。
2) gh run view <RUN_ID> --log --repo racheko-lab/blive-monitor | grep -iE "kuaishou|预热|warmup|result=2|捕获稳定游客|注入稳定游客|kwfv1|kwssectoken|kwpsecproductname" | tail -80
   重点看：warmup 是否报 success？是否有"[kuaishou] 捕获稳定游客 did="和"[kuaishou] 注入稳定游客身份 cookie（N 条，did=...）"？注入的 cookie 里**有没有 kwfv1 / kwssectoken / kwscode**？作品 fetch 调用前这些 token 在不在 context 里？
3) gh api repos/racheko-lab/blive-monitor/contents/kuaishou_guest_visitor.json --jq '.content' | base64 -d | python3 -m json.tool
   确认 did 稳定，并看 cookies 数组里有哪些 name（预期只有 kpf/clientid/did/ktrace-context/kpn/kwpsecproductname，没有 kwfv1/kwssectoken/kwscode）。
4) 读 kuaishou_feed.py 的 _wait_visitor_cookies：它到底在等哪些 cookie 才认为预热成功？是不是只等了 did，没等风控 token？这是 H1 假设的核心。

输出：用中文给出你支持的假设（H1~H4 中哪个），每条都附"日志/代码证据"。如果没有足够证据，明确说还差什么。
```

---

## 提示词 3 —— 进入修复实现

```
基于诊断结论实现修复。硬性约束：
- 保持匿名无 cookie 设计：不要往仓库提交任何用户 cookie / token / Secret。增强风控抵抗优先走 visitor JS 预热完整性 或 BLIVE_CONFIG.browser_proxy（出口代理），而不是硬编码凭证。
- 不要破坏 did 跨运行复用：kuaishou_guest_visitor.json 应继续稳定非 null。
- 若需要让 warmup 等到 kwfv1/kwssectoken/kwscode 再判定成功（H1），注意这些 token 次数/时效受限——不要把它们写进 kuaishou_guest_visitor.json 跨运行复用（那会快速失效），应是"每次 warmup 现生成、本次会话内使用"。
- 改完跑 pytest tests/test_kuaishou_feed.py；如引入新行为，补对应单测。

实现后直接 push 到 master（你直连，无需代理）。推送后等 ≤5 分钟让 CI 跑一轮并重新部署 Pages（或 gh workflow run check.yml 手动触发并 gh run watch 盯日志）。commit message 用 Conventional Commits 风格、中文描述亦可。
```

---

## 提示词 4 —— 验证与收尾

```
验证修复是否真的生效（你直连，直接查）：
1) gh workflow run check.yml --repo racheko-lab/blive-monitor 然后 gh run watch，确认 kuaishou 作品 fetch 不再连续 result=2；或等 schedule 自然跑。
2) 盯几次 run 日志，确认 warmup 成功、作品列表能拿到（result=1 且有列表），无 [2,2,...] 序列。
3) 确认 monitor.html 角标版本号已变成你新 commit 的 SHA（说明修复已部署上线）。
4) 确认 kuaishou_guest_visitor.json 的 did 仍稳定非 null（复用未被破坏）。
5) 在仓库内更新或另写一份简短结论（根因 + 修复 + 验证证据），并视情况更新 HANDOVER_KUAISHOU_2026-08-15.md 的"当前未决任务"为已解决。

完成标准：线上"通知健康"面板 kuaishou 账号不再持续 result=2；did 复用稳定；单测通过；有一份实证结论。达标后向我报告。
```
