# 快手监控交接文档（2026-08-15）

> 交接时间：2026-08-15 19:46 GMT+8（11:46 UTC）
> 仓库：`racheko-lab/blive-monitor`（公开 GitHub Pages），分支 `master`
> 接手 agent 环境：**可直接连 GitHub，无需代理**（见 §1 重要说明）
> 当前线上版本号（monitor.html 角标）：`v13a1fc8 · 2026-08-15 11:45 UTC`

---

## 0. 一句话现状

快手的 **`did` 跨运行稳定复用已修好且未回归**（身份 cookie 持续复用，gated 不再累积）。但 **kuaishou 作品接口（新作品监控）从 08-14 23:19 起持续 `~20h` 被风控 `result=2`（预热未通过）**，单轮重试 14 次全挂。这是本次交接的**核心未决任务**。

---

## 1. ⚠️ 重要：接手 agent 的环境说明

**上一个 agent（本交接的产出方）运行在受限沙箱里**：GitHub 只能走 `ghproxy.net` 代理、`gh` REST API 被墙、不能 `workflow_dispatch` 触发 CI。

**你（接手 agent）的环境与之相反——直连 GitHub，无任何代理限制。** 请务必按"直连"假设工作，不要继承任何"API 被墙 / 不能触发 CI"的错误前提：

| 能力 | 你可用（直连） | 上一个 agent（受限沙箱） |
|---|---|---|
| `gh` CLI | ✅ 已登录 / 可直接用 token | ⚠️ 仅 git 协议经代理 |
| `git push` | ✅ 直连 `github.com` | ⚠️ 必须 `ghproxy.net` |
| 触发 CI | ✅ `gh workflow run check.yml` | ❌ 无 `workflow:write` |
| 看 CI 日志 | ✅ `gh run view <id> --log` | ❌ |
| GitHub REST API | ✅ `gh api` 可读写、可写 Secret | ❌ 被防火墙掐断 |
| Contents API 写文件 | ✅ 可直接写 | ❌ |

> 因此你可以做的事远多于上一个 agent：手动触发并实时盯 CI 日志、用 `gh api` 直接读历史状态文件、必要时写 Secret。充分利用。

---

## 2. 已完成的工作（均已推 `master`）

| 项 | 内容 | 状态 |
|---|---|---|
| 快手 `did` 跨运行复用 | `5f02695f`：`kuaishou_feed.py` 的 `_cycle` 末尾**兜底捕获** `did`（覆盖"did 仅在 profile 导航才种下、warmup 看不到"的海外出口场景）；`_capture_visitor_cookies` / `_apply_visitor_cookies` 注入稳定身份 | ✅ 生效，did 零漂移 |
| CI 防回退保护 | `19a4f1ef`：`check.yml` 仅当 `kuaishou_guest_visitor.json` 含有效 `web_` 前缀 did 才采用，否则回退已提交版本 | ✅ |
| 前端版本号 | monitor.html 右下角角标 + index.html 页脚，CI 部署时自动用 `git rev-parse --short HEAD` + UTC 时间烙印（`v<SHA> · <时间>`），无需手动 bump | ✅ 部署生效 |
| 多日验证 | 08-07~08-14 回溯 + 28 分钟实时轮询：`did` 跨多日零漂移、`gated_count` 全账号峰值恒 1 | ✅ |

---

## 3. 🔴 当前未决任务（核心交接）

### 3.1 现象
- 线上 `monitor.html` → "通知健康"面板，自 **08-14 23:19 UTC** 起持续出现：
  > 快手匿名通道被风控（快手作品接口未返回列表（**result=2：风控预热未通过**，通常重试下一轮即可）：未拿到作品列表（响应序列=`[2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`））
- 受影响账号（kuaishou）：`wikw-0403-`、`HUizi-G-H`、`wadaxiwachangmingran`（早前还有 `Sandy88888`、`ii050824`）。
- 持续至 **08-15 19:06 UTC**（约 20 小时），非瞬时抖动。仪表盘"通知异常: 26"。
- 同一时段 `did` 缓存**完全稳定**（`web_d6d698fea12a0722d654539cc323b3f5` 未变）。

### 3.2 已排除 / 已厘清
- ❌ **不是 `did` 复用回归**：`kuaishou_guest_visitor.json` 的 `did` 从 08-14 13:52Z 起零漂移，捕获/注入链路正常。
- ⚠️ **`result=2 预热未通过` 指向风控 token 层，而非 `did` 层**：`5f02695f` 故意**不**跨运行持久化 `kwfv1` / `kwssectoken` / `kwscode`（次数/时效受限）。作品接口要过风控，很可能依赖这些在**每次 warmup 由 visitor JS 现生成**的 token；若 warmup 没真正拿到它们，就会 `result=2`。
- ⚠️ **两个指标是不同代码路径，别混淆**：
  - `post_tracking.json` 里的 `gated_count` / `did_used` 来自 **`backend/adapters/kuaishou.py`**（直播状态监控，走 `CredentialLevel` ladder）—— 这条稳定。
  - 当前的 `result=2` 来自 **`backend/adapters/kuaishou_feed.py`**（新作品监控，`check_new_posts.py` 实际调用它）—— 这条在挂。
  - 换言之：`did_used` 字段是否 `True` 与本次作品接口 gating **无关**。

### 3.3 待新 agent 验证的假设
- **H1（最可能）**：`kuaishou_feed.py` 的 warmup（`_wait_visitor_cookies` / `_warmup`）只等 `did` 种下，没等 `kwfv1/kwssectoken/kwscode` 现生成，导致作品接口调用时缺 token → `result=2`。
- **H2**：海外 Azure IP 信誉在 did 稳定后仍被快手硬 gate，与 token 无关（需换出口/代理验证，见 `BLIVE_CONFIG.browser_proxy`）。
- **H3**：`_cycle` 兜底捕获只在结束时抓，注入的 `did` 没真正进到"作品 fetch"所用的 context。
- **H4**：visitor JS 在 Azure 出口下 warmup 偶发失败，需看 CI 日志确认 warmup 是否报 success。

---

## 4. 关键文件索引

| 文件 | 作用 |
|---|---|
| `backend/adapters/kuaishou_feed.py` | **本次问题主战场**：新作品监控、`_warmup` / `_wait_visitor_cookies` / `_cycle` / `_capture_visitor_cookies` / `_apply_visitor_cookies` |
| `kuaishou_guest_visitor.json`（仓库根） | `did` + 身份类 cookie 缓存；CI 跨运行保留。当前 `did=web_d6d698fea12a0722d654539cc323b3f5` |
| `post_tracking.json` | 每账号 `gated_count` / `credential_level` / `did_used`（**live 状态路径**，非作品接口） |
| `check_new_posts.py` | 作品监控编排，调用 `kuaishou_feed` |
| `backend/adapters/kuaishou.py` | 直播状态监控（独立的 `CredentialLevel` ladder），与本次问题无关但别误改 |
| `.github/workflows/check.yml` | CI：`Build Pages` 步骤含 `Stamp frontend version`（烙版本号）；`web_` 前缀保护逻辑 |
| `monitor.html` / `index.html` | 前端；版本角标在 monitor.html 底部 tab 栏**正上方**（之前被 tab 栏遮挡已修） |

---

## 5. 关键坑（务必先看）

1. **`did_used` 字段误导**：它在 `post_tracking.json`，属 `kuaishou.py`（live）路径，与作品接口 gating 无关。别拿它判断本次问题。
2. **两个 `result=2` 路径不同**：作品接口（`kuaishou_feed.py`）vs live 状态（`kuaishou.py`）。本次是前者。
3. **`Nizi981116` 是已知地域问题，非回归**：海外 Azure 出口下该账号作品列表缺最新一条（大陆出口正常），与风控无关；基线防回退逻辑已处理。
4. **CI 触发器**：`on: schedule(*/5) + workflow_dispatch + push(仅 rooms.json/post_rooms.json)`。改 `monitor.html`/`kuaishou_feed.py` **不会**自动触发——靠 schedule 每 5 分钟一轮（flaky，可能延迟），或你手动 `gh workflow run check.yml`。
5. **Pages 部署时机**：每次 workflow 跑完 `check` → `deploy` 作业把 `_site` 发到 Pages。你 push 后等 ≤5 分钟（或手动触发）即上线；monitor.html 角标会显示新 `v<SHA>` 供你确认"修复已上线"。
6. **前端版本号全自动**：部署时由 CI 用 commit 短 SHA 烙入，**别手动改版本号**，也别把 `__APP_VERSION__` / `__APP_CACHE_BUST__` 占位符留在产物里（CI 会替换）。
7. **无登录 / 匿名是硬约束**（用户明确要求"免登录无 cookie"）：不要让修复依赖把用户 cookie 提交进仓库。增强风控抵抗优先走 visitor JS 预热 / `BLIVE_CONFIG.browser_proxy` 代理出口，而非提交 cookie。

---

## 6. 给新 agent 的诊断起点（你直连，直接干）

```bash
# 1) 看最近 CI 运行，找 result=2 出现的 run
gh run list --repo racheko-lab/blive-monitor --limit 15

# 2) 看某次 run 的日志，grep kuaishou warmup / 捕获 / 注入 / result=2
gh run view <RUN_ID> --log --repo racheko-lab/blive-monitor | grep -iE "kuaishou|预热|warmup|result=2|捕获稳定游客|注入稳定游客|kwfv1|kwssectoken" | tail -60

# 3) 直接读当前 did 缓存（确认稳定）
gh api repos/racheko-lab/blive-monitor/contents/kuaishou_guest_visitor.json --jq '.content' | base64 -d | python3 -m json.tool

# 4) 读某次 commit 的 post_tracking，看 gated_count 历史（live 路径，仅供参考）
gh api repos/racheko-lab/blive-monitor/contents/post_tracking.json --jq '.content' | base64 -d | python3 -c "import sys,json;d=json.load(sys.stdin);[print(k, v.get('gated_count'), v.get('credential_level')) for k,v in d.items() if k.startswith('kuaishou')]"

# 5) 手动触发一次并实时盯（你直连才做得到）
gh workflow run check.yml --repo racheko-lab/blive-monitor
gh run watch --repo racheko-lab/blive-monitor
```

---

## 7. 完成标准（Definition of Done）

- [ ] 定位 `result=2（预热未通过）` 根因（H1~H4 中哪一个），有 CI 日志/代码实证，不是猜测。
- [ ] 修复后，线上"通知健康"面板 kuaishou 账号**不再持续 `result=2`**（允许偶发、但应"重试下一轮即可"自愈，而非 14 连挂）。
- [ ] 不破坏既有 `did` 跨运行复用（缓存仍稳定非 null）。
- [ ] 保持匿名无 cookie 设计；若引入代理出口增强，走 `BLIVE_CONFIG.browser_proxy` Secret，不提交凭证。
- [ ] 单测通过（`pytest tests/test_kuaishou_feed.py`），必要时补测试覆盖新行为。
- [ ] 交付一份简短结论（根因 + 修复 + 验证），更新本交接文档或另写。

> 注：上一个 agent 的更早交接见仓库内 `HANDOVER_KUAISHOU_2026-08-13.md`（含 Nizi981116 地域问题、双 adapter 架构等历史上下文），本文件只覆盖 08-15 的现状与未决任务。
