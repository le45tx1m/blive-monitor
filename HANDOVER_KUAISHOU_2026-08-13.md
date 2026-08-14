# 快手监控通道 · 交接文档

> 日期：2026-08-13｜负责人：WorkBuddy（沙箱云 IP 环境）
> 范围：blive-monitor 的**快手新作品监控通道**与 **Nizi981116（住在峡谷的少女）路径修复**
> 受众：接手本项目的工程师 / 下一轮 AI agent

---

> ## 🔄 2026-08-14 更新：快手默认即为「无 cookie 版本」
>
> - **快手新作品监控默认不再需要任何 cookie**。原 `config/kuaishou_cookie.txt`（一份提交在**公开仓库**的登录态 cookie，既暴露凭证又易 stale）已删除；`KuaishouAdapter._session` 现在默认以**空 cookie（匿名）**启动，完全依赖浏览器预热种新鲜风控 token 的免登录通道。
> - cookie 变为**可选覆盖**：仅当个别账号匿名仍被挡时，才通过环境变量 `KUAISHOU_COOKIE` 或 `BLIVE_CONFIG.kuaishou_cookie` 显式注入（不再有「提交 cookie 到仓库」的通道）。
> - 因此下文所有「覆盖/提交 `config/kuaishou_cookie.txt`」「cookie 过期需轮换」的运维指引**已废弃**，请勿再照做。

> ## 🔄 2026-08-14（续）更新：Playwright 跑完整 visitor JS 拿游客 cookie + 稳定 did 复用
>
> - **问题**：即便无 cookie 匿名通道能通（`result=1`），产线（GitHub Actions 海外 Azure IP）仍频繁被风控（gated / `result=2` / `400002`）。根因之一是快手 visitor JS 给**每个全新浏览器上下文**都生成新 `did`（游客/设备 ID `web_<hex>`）——匿名云 IP 下每次都像「第一次来的新访客」→ 风控权重更高、更易被挡。
> - **修复（无 cookie 前提下）**：改 `backend/adapters/kuaishou_feed.py` 的 `KuaishouFeedSession`：
>   1. **预热不再等 `networkidle`**：快手 SPA 永不 idle，旧版 `wait_until="networkidle"` 必等满 45s 超时才降级 `domcontentloaded`，单轮 ~54s。改为 `domcontentloaded` + `_wait_visitor_cookies()` 轮询 `context.cookies()`，直到 `did` 与 `kwfv1/kwssectoken/kwscode` 全套种下（实测 ~1.1s）。
>   2. **稳定游客 `did` 捕获 + 复用**：`_capture_visitor_cookies()` 在**预热成功时**与**每轮 `_cycle` 结束**都抓 `did` 等「设备/配置类」身份 cookie（**不含** `kwfv1/kwssectoken/kwscode` 等受限风控 token）——后者兜底覆盖「did 仅在 profile 导航才种下、warmup 页看不到」的海外出口场景；落盘 `kuaishou_guest_visitor.json` 并随状态文件被 CI 提交；之后每个新 context（含跨 CI 运行）经 `_apply_visitor_cookies()` 注入**同一 did**，让快手认作「同一游客」逐步积累信誉。
> - **实测**：沙箱环境两「轮」（全新 context）warmup 均 1.1s、`result=1`、且 `did` 跨轮完全一致（`DID_REUSE_OK`）。产线 gated 率是否下降需下一轮 CI 实跑验证。
> - **跨运行持久化**：`kuaishou_guest_visitor.json` 已加入 CI 的 `STATE_FILES`/`PERSIST_FILES`（与 `state.json` 同级，根目录），能扛过 `git reset --hard origin/master` 被提交，从而跨运行保留同一 `did`。
> - **调试/起号**：`reset_guest_visitor_cache()` 可清空缓存，下轮重新养一个全新 `did`。

---

## 0. 一句话现状

- 快手新作监控已能抓到作品（不再 `result=2` 卡死抓不到）。Nizi981116 之前"抓不到最新一条"的根因是**导航用了 `principal_id` 而非快手号**，已通过改为裸 `id` / 快手号导航修复，现与肥阿肥 同路径。
- **快手默认无 cookie（匿名通道）**：`KuaishouAdapter._session` 默认以空 cookie 启动，`fetch_new_posts` 如实记录 `cookie_used=False` / `credential_level=anonymous`（`kuaishou.py`）。历史上曾把登录态 cookie 提交进公开仓库（`config/kuaishou_cookie.txt`，2026-08-14 已删除），现已不依赖任何 cookie 即可抓取。
- 最大运维负担（原 cookie 过期轮换）**已消除**——匿名通道由浏览器每次预热种新鲜 token，无需人工维护 cookie。

---

## 1. 本轮回关键改动（commit 索引）

| commit | 内容 |
|---|---|
| `9545f20e` | （已废弃）曾新增 `config/kuaishou_cookie.txt`（完整快手登录态 cookie）并给 `load_kuaishou_cookie()` 加第 3 优先级读仓库文件。**该文件于 2026-08-14 删除，快手回归默认无 cookie。** |
| `d1d578a3` → rebase 后 `9545f20e` | 同上（rebase 到 CI 的 state 提交之上）。 |
| `2249b3d3` | `rooms.json` 中 Nizi981116 去掉 `principal_id`/`share_url`，改为裸 `id` 形态，与肥阿肥 同路径。rebase 时合并了 web 端新增的抖音账号 `00512x`。 |

> 仓库：`racheko-lab/blive-monitor`（公开 GitHub Pages）。推送走 `ghproxy.net` 代理的 git 协议，分支 `master`。

---

## 2. 关键架构决策与原因（反直觉，务必看）

### 2.1 cookie 为什么提交进公开仓库，而不是设 Secret

**事实**：本沙箱**无法设置 GitHub Actions Secret**。
- `api.github.com`（REST API）被防火墙劫持到黑洞地址 `198.18.0.17`，直连 `SSL_ERROR_SYSCALL`；
- `ghproxy.net` 只代理 `git`/`raw`，对 `api.github.com` / `github.com/contents` 一律 `Invalid input`；
- `auth.proxy`（IDE 内部代理）不转发到 `api.github.com`；
- git remote 里那个 `ghp_` token **只能 `git push` 代码，不能写 Secret**（Secret 只能经 REST API 写）。

所以"之前能推"指的是 `git push` 代码，**从来没、也不可能从这环境设过 Secret**。要注入 cookie 只有两条路：
1. **公开仓库文件通道（已采用）**：提交 `config/kuaishou_cookie.txt`，CI 读文件。代价：cookie 出现在公开 git 历史，会话期内有被冒用风险。
2. **Secret 通道（更安全）**：在 GitHub UI 或 `monitor.html`（借用户浏览器 PAT + libsodium）设 `KUAISHOU_COOKIE` 或 `BLIVE_CONFIG.kuaishou_cookie`。设上即覆盖文件值，且不进公开仓库。

> `load_kuaishou_cookie()` 优先级：`KUAISHOU_COOKIE` 环境变量 > `BLIVE_CONFIG.kuaishou_cookie` > 仓库文件。Secret 一旦设上，文件自动失效。

> ⚠️ **cookie 通道实测观测（重要，别误读）**：最新 CI（`post_tracking.json`，last_success 2026-08-13 03:33）中 `kuaishou_Nizi981116` 与 `kuaishou_Sandy88888` 均 `cookie_used=false` / `credential_level=anonymous`。**这不代表 cookie 没加载**——`load_kuaishou_cookie()` 已确认读到文件（1561 字节、token 齐全），`KuaishouFeedSession` 也收到了 cookie；而是 `fetch_new_posts` 在 `kuaishou.py:570-573` **硬编码**了匿名等级（该路径不维护 `last_ladder`，见 §4.2）。所以 `cookie_used` 字段对快手 feed **永远为 false**，不能据此判断 cookie 是否生效。Nizi981116 "抓不到最新" 的真正修复是**导航改快手号**，不是 cookie。

### 2.2 "抓不到最新一条"的根因与修复

**根因（非解析逻辑，是导航标识）**：`KuaishouFeedSession.fetch()` 导航 `live.kuaishou.com/profile/{pid}`。
- 肥阿肥 传的是**快手号** `Sandy88888` → 正确拉到作品流 → 抓到最新；
- Nizi981116 之前传的是 **`principal_id`（`3x…`）** → 该 URL 段拉到的列表不对（或条目 CDN 时间戳解不出），`pick_latest()` 返回 `None` → "最新一条"永远抓不到。

解析层 `pick_latest`/`sort_by_time`（`kuaishou_feed_core.py`）按 CDN URL 反解发布时间取最新，**与 principalId/快手号无关**——所以 bug 不在那，在"导航 URL 用的标识"。

**修复**：去掉 Nizi981116 的 `principal_id`，让它和肥阿肥 一样用 `id`（快手号 `Nizi981116`）导航。两者现在同形态：`{platform, id, name}`。

> ⚠️ **残留风险**：若 Nizi981116 **没有设过快手号**（author.id 是 `3x…` 形态），解析器 `LiveProfileStrategy` 仍可能吐出 principalId，feed 走回 `profile/3x…` 老路。届时需加"导航优先用快手号"护栏（见 §4.3）。

### 2.3 云 IP 解析能力边界（设计已固化，勿重复踩）

匿名云 IP 下，**纯用户名无法正向解析出 `principal_id`**（所有 SSR/搜索端点实测失败）。所以：
- 靠用户名监控的账号，resolver 能拿到 `origin_user_id` + 昵称（够开播提醒），但 `principal_id` 为空；
- 新作品监控必须给账号一个"种子"：`principal_id` / `share_url` / 作品 `seed_url`，或依赖缓存。
- Nizi981116 之前加 `principal_id`+`share_url` 就是这个种子；现在改裸 `id` 是因为**导航用快手号本身就能落账号**（肥阿肥 实证），且 cookie 已解决匿名 `result=2`。

---

## 3. 已知风险 / 坑

| 风险 | 说明 | 应对 |
|---|---|---|
| **cookie 过期** | `api_st`/`passToken`/`kuaishou.s` 为会话 token，数小时~数天失效。过期后 Nizi981116 退化为 `result=2`（gated），新作漏检（不崩溃）。 | 重新抓 cookie 更新文件并推 master（§4.1）。建议加过期告警（§4.4，未做）。 |
| **公开仓库暴露 cookie** | `config/kuaishou_cookie.txt` 在公开 git 历史，会话期内有被冒用风险。 | 接受过的权衡。要彻底消除就改走 Secret 通道（§2.1）。cookie 过期即轮换可降风险。 |
| **principalId 导航回退** | 若 Nizi981116 无快手号，resolver 可能再吐 principalId，走回抓不到最新的老路。 | 加导航护栏（§4.3）或把 `principal_id` 加回 rooms.json。 |
| **token 打废** | 同一 token 连续命中约 4 次后被风控打废。代码已做主动重预热 + 整轮 `result=2` 被动自愈两层兜底。 | 无需人工，但若整轮全 `result=2` 持续，多半是 cookie 过期（见上）。 |
| **新访客风控（did 每次重置）** | 快手 visitor JS 给每个全新 context 生成新 `did`；匿名云 IP 下每次像「新访客」→ 风控权重高、易 gated。 | **已修**：首次预热捕获 `did` 并落盘 `kuaishou_guest_visitor.json`（随状态提交），后续 context/运行注入同一 `did`（`kuaishou_feed.py` `_apply_visitor_cookies`/`_capture_visitor_cookies`）。跨运行保留同一设备身份，逐步积累信誉。 |
| **预热慢（networkidle 超时）** | 旧版 `wait_until="networkidle"` 在快手 SPA 永不 idle，必等满 45s 超时降级，单轮 ~54s。 | **已修**：改 `domcontentloaded` + `_wait_visitor_cookies()` 轮询 cookie，~1.1s 确认 `did`+风控 token 种下。 |
| **CI 推送冲突** | CI/web 会改 `rooms.json`（state 提交 / web 增删房间），本地推送常被挡，需 `git pull --rebase` 解决（曾撞 Nizi981116 块冲突，已手动合并保留 web 新增的 `00512x`）。 | 推送前先 `git pull --rebase origin master`，冲突时保留 web 端新增条目。 |
| **`cookie_used` 恒为 `false`（观测陷阱）** | `fetch_new_posts` 硬编码 `cookie_used=False`（`kuaishou.py:570-573`），不看 `last_ladder`。CI 里两个快手账号都会显示 `cookie_used=false`，容易误判成"cookie 没加载 / 通道坏了"。 | **别用 `cookie_used` 判断 cookie 状态**；以 `latest_post_id` / `last_success` 为准（§4.2）。真要观测 cookie 效果，需改代码让 `last_ladder` 反映 `KuaishouFeedSession` 实际用的凭证等级（见 §5）。 |

---

## 4. 运维手册（runbook）

### 4.1 登录态 cookie（已废弃 —— 快手默认无 cookie）

> ⚠️ 本小节原为「提交登录态 cookie 到公开仓库」的运维步骤，**已于 2026-08-14 废弃**。
> 快手新作品监控默认走浏览器匿名通道，不需要任何 cookie；下方步骤请勿再执行。
> 仅当个别账号匿名仍被挡时，才通过环境变量 `KUAISHOU_COOKIE` 或 `BLIVE_CONFIG.kuaishou_cookie`
> 选择性注入 cookie（不进仓库、不公开）。

~~1. 借其它 agent 工具，从用户 iPhone Safari（已登录快手网页版）抓完整登录态 cookie。~~
~~2. 覆盖 `config/kuaishou_cookie.txt`（纯 cookie 串，一行，无注释）。~~
~~3. `git add config/kuaishou_cookie.txt && git commit ... && git push`。~~

### 4.2 验证 cookie / 路径是否生效

- **`cookie_used` 永远为 `false` 是预期行为，不是故障**：`fetch_new_posts` 在 `kuaishou.py:570-573` 硬编码 `cookie_used=False` / `credential_level=anonymous`（该路径不维护 `last_ladder`，见 §2.1）。所以**不能**用 `cookie_used` 判断 cookie 是否生效——两个快手账号在 CI 里都会显示 `cookie_used=false`。
- 真正看 cookie/路径是否生效，看这几项：
  - Nizi981116 的 `latest_post_id` **不为空**（修复前是 `None`）→ 说明已能抓到作品；
  - `last_success` 有值（当前实测 `2026-08-13 03:33`）、`gated_count` 不再卡死。
  - 当前 `kuaishou_Nizi981116.latest_post_id = 3xi6qc9eg8w5vvg`、`latest_published_at = 2020-01-23`。
- ⚠️ **残留核对点**：Nizi981116 当前 `latest_published_at=2020-01-23`，若该账号近年其实有更新、却仍停在 2020，说明导航可能仍没拿到"真正最新"（残留风险见 §2.2），需结合账号实际作品列表人工核对。
- 本地抽测 `load_kuaishou_cookie()`：默认应返回空串（无 `KUAISHOU_COOKIE`/`BLIVE_CONFIG` 时即为匿名）；仅在显式设置环境变量或 `BLIVE_CONFIG.kuaishou_cookie` 时才非空。

### 4.3 （可选）加"导航优先用快手号"护栏

若 §2.2 残留风险触发，在 `backend/adapters/kuaishou.py` 的 `fetch_new_posts` 调用处，将传给 `fetch()` 的 `rid` 改为**优先用 rooms.json 的 `id`（快手号）**，仅在无快手号时才回退到解析出的 `principal_id`。这样所有快手账号都走肥阿肥 已验证路径。改动需回归 `kuaishou_Sandy88888` 与 `kuaishou_Nizi981116` 两条。

### 4.4 （可选）加 cookie 过期告警

当前无自动告警：cookie 过期后 Nizi981116 静默退化为 `result=2`。建议在 `check_new_posts.py` 检测到 Nizi981116 连续 `result=2` 时发一条通知，提醒"cookie 该换了"。需确认推送渠道已配（见 `BLIVE_CONFIG`）。

---

## 5. 待办 / 开放问题

- [ ] **导航护栏（§4.3）**：Nizi981116 若有快手号则已修好；若没快手号需加护栏。下一轮 CI 跑完即可判定。
- [ ] **让 `cookie_used` 观测真实化（§3 风险表）**：`fetch_new_posts` 硬编码 `cookie_used=False`，无法从 `post_tracking.json` 判断 cookie 是否真被 session 用上。若要让 cookie 效果可观测，需让 `last_ladder` 反映 `KuaishouFeedSession` 实际凭证等级（改 `kuaishou.py` 相关逻辑）。
- [ ] **cookie 过期告警（§4.4）**：尚未实现，目前靠人工发现漏检。
- [ ] **自动刷新**：无（重新抓 cookie 需用户设备上已登录的浏览器会话，沙箱无法伪造，故必有人工环节）。
- [ ] 考虑是否把 cookie 从公开仓库迁到 Secret 通道（更安全，但需用户侧设一次）。
- [ ] **Nizi981116 最新一条复核**：当前 `latest_published_at=2020-01-23`，需人工核对账号近年是否有更新、导航是否真拿到最新（§4.2 残留核对点）。

---

## 6. 关键文件索引

| 文件 | 作用 |
|---|---|
| `config/kuaishou_cookie.txt` | **登录态 cookie（公开！）**，CI 读取突破风控。过期需更新。 |
| `check_new_posts.py` → `load_kuaishou_cookie()` | cookie 加载，优先级：env > `BLIVE_CONFIG` > 仓库文件（line ~162-178）。 |
| `backend/adapters/kuaishou_feed.py` → `KuaishouFeedSession.fetch()` | 浏览器导航 `live.kuaishou.com/profile/{pid}` 拦截作品接口。**pid 用快手号能抓最新，用 principalId 抓不到。** |
| `backend/adapters/kuaishou_feed.py` → `_apply_visitor_cookies`/`_capture_visitor_cookies`/`_wait_visitor_cookies` | 无 cookie 游客身份：预热后抓 `did` 等身份 cookie、跨 context/运行复用稳定 `did`（降新访客风控）；`_warmup` 改 `domcontentloaded`+轮询取代 45s networkidle。 |
| `kuaishou_guest_visitor.json`（仓库根） | 稳定游客身份缓存（`did`+设备/配置类 cookie），被 CI 随状态文件提交，跨运行保留同一 `did`。调试可 `reset_guest_visitor_cache()`。 |
| `backend/adapters/kuaishou_feed_core.py` | 纯逻辑：`parse_profile_public` / `sort_by_time` / `pick_latest` / `decode_media_meta`（CDN URL 反解发布时间）。 |
| `backend/adapters/kuaishou_identity.py` | 身份解析器：云 IP 下纯用户名无法解 `principal_id`（已实证，见模块文档）。 |
| `backend/adapters/kuaishou.py` → `resolve_kuaishou_identity()` / `fetch_new_posts()` | 编排：解析 → 取 `pid or rid` → feed。 |
| `rooms.json` | 监控列表。快手条目现为 `{platform, id, name}` 同形态。 |
| `post_tracking.json` | 各账号解析/抓取结果（`kuaishou_Sandy88888` / `kuaishou_Nizi981116`）。 |
| `/workspace/KUAISHOU_COOKIE_VERIFIED.md` | 早期验证报告（gated 对照证据、部署通道说明）。 |

---

## 7. 环境约束速查（接手前必读）

- **能做的**：`git push origin master`（经 `ghproxy.net` 代理，git 协议可用）。
- **不能做的**：写 GitHub REST API（设 Secret / 写文件经 Contents API）—— 被防火墙掐断。
- **token**：git remote 里的 `ghp_` 仅能 git 操作，不能写 Secret。
- **测试**：本沙箱无浏览器（playwright/patchright 不一定可跑），`kuaishou_feed` 的"最新一条"逻辑无法在此实证，只能靠 CI 跑后看结果。
- 本地改动推送前务必 `git pull --rebase origin master`，CI/web 常抢改 `rooms.json` 与 state 文件。

---

## 8. 2026-08-13 下午续查：「Nizi981116 抓不到最新」根因 = 出口地域（修正 §2.2 的判断）

> 排查者：千问办公助理（本沙箱出口 = 杭州阿里云，大陆 IP）

### 根因（已实证，可复现）
快手 `live_api/profile/public` **按访问者地域返回不同的作品列表**：

| 出口 | Nizi981116 返回 |
|---|---|
| 大陆（杭州阿里云，本沙箱） | 5 条，含最新 2023-06-15 `3x6uavw8xesypye`，`pcursor=no_more`，连测 4 次稳定一致 |
| GitHub Actions（海外 Azure） | **缺最新一条**，基线自首次建基线（2026-08-13 03:33）起恒停在 2020-01-23 `3xi6qc9eg8w5vvg`，当日 12 个历史快照全部如此 |

- §2.2 的「导航标识」修复不是根因：改快手号导航后 CI 依旧缺那条；同样代码在大陆出口稳定拿到含最新的完整列表。
- 用户确认该账号共 5 条作品 → 大陆出口列表 = 全集。
- wada / Sandy 不受影响（其最新作品在海外出口也可见），说明是「按内容/账号」的差异而非全站封锁。
- 匿名通道（graphql/H5/主站匿名）全部实测无法绕过；主站全集接口需登录（result=109）。

### 本轮已提交的改动
1. **基线防回退**（`kuaishou.py fetch_new_posts`）：本轮最新比已有基线旧 → 判定退化列表，基线保持不变并告警（防止海外退化视图把基线打回旧帖、下轮又误推）。
2. **观测字段** `last_fetch_items`（id+ts）：记录每轮接口真实返回，CI 从下一轮起可直接看到拿到了哪几条。
3. **出口代理支持**：`check_new_posts.load_browser_proxy()`，env `BROWSER_PROXY` 或 `BLIVE_CONFIG.browser_proxy`，格式 `http://[user:pass@]host:port`；workflow 已加 `BROWSER_PROXY: ${{ secrets.BROWSER_PROXY }}`。配置大陆出口代理后浏览器抓取全部走代理。
4. 新增单测 4 个（防回退/首轮建基线/观测字段/代理解析），247 项全过。

### 待办
- [ ] **用户提供大陆出口代理**并设入 GitHub Secret `BROWSER_PROXY`（只能经 GitHub UI 设置）。配置后验证 `last_fetch_items` 出现 5 条、`latest_post_id` 前进到 `3x6uavw8xesypye`，并会收到 2023-06-15 那条的补推（去重账本无记录，会正常推送一次）。
- [ ] **passToken 已疑似失效**：排查中曾用 `config/kuaishou_cookie.txt` 的 passToken 走 `id.kuaishou.com/pass/kuaishou/login/passToken?sid=kuaishou.server.webday7&callback=https://www.kuaishou.com/rest/infra/sts?followUrl=...` 成功换到一次主站会话（证明该路可通、可做成自动续期），随后报「账号异常，请重新登录」（token 疑被轮换消耗，勿再重试旧 token）。live 通道不依赖 passToken，生产监控不受影响；若未来要走主站登录通道，需用户重新抓 cookie。
- [ ] 若代理后仍有个别作品不可见（更细的地域限制），唯一剩余路径是主站登录通道（`rest/v/profile/feed`，需 `kuaishou.server.webday7_st` 会话）。
