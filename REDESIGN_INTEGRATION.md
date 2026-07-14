# blive-monitor 重构原型 · 接入说明

## 一、Mock 数据在哪里
位于文件 `<script>` 顶部的 `const data = {...}`（约第 448 行）：
- `data.rooms` —— 直播监控房间
- `data.post_rooms` —— 新作监控账号
- `data.history` —— 监控日志

另有两个辅助映射：`PLATFORM_NAME` / `PLATFORM_COLOR`（平台显示名与色）、`LOG_TYPE`（日志类型 → 徽标样式）。渲染函数 `renderLive / renderPosts / renderLog / renderDashboard` 均直接读取 `data.*`。

## 二、真实接入改什么
把 `const data = {...}` 这段硬编码，替换为异步拉取仓库内的静态 JSON（字段已对齐，无需改渲染层）：

```js
async function loadData(){
  const t = Date.now();
  const [rooms, post_rooms, history] = await Promise.all([
    fetch('rooms.json?_='+t).then(r=>r.ok?r.json():[]),
    fetch('post_rooms.json?_='+t).then(r=>r.ok?r.json():[]),
    fetch('history.json?_='+t).then(r=>r.ok?r.json():[]),
  ]);
  Object.assign(data, {rooms, post_rooms, history});
  renderLive(); renderPosts(); renderLog(); renderDashboard();
}
loadData();
```

## 三、字段对应关系
- `rooms.json` → `data.rooms[]`：`platform / id / name / enabled / live / online`
- `post_rooms.json` → `data.post_rooms[]`：`platform / id / name / enabled / lastTitle / lastTime`
- `history.json` → `data.history[]`：`type / name / title / time`

## 四、CORS 注意点（关键）
- 这些 JSON 由 GitHub Actions 推送到仓库，由 GitHub Pages **同源静态托管**。用相对路径 `fetch('rooms.json')` 即可，浏览器视为同源请求，**不存在跨域问题**（参考现有 `monitor.html` 第 4003–4006 行）。
- 仅当要**写回仓库**或读取私有内容时才请求 `api.github.com`，那才需要 Bearer Token 且涉及 CORS。该域名支持 CORS、大陆可直连，但**不要加 `Cache-Control` 等非 CORS 安全头**，否则预检失败、请求被拦（详见现有 `monitor.html` 第 3179–3181 行注释）。
- 建议给每个 `fetch` 加 `AbortSignal.timeout(8000)` 与 `.catch` 兜底，保证单文件离线打开也不白屏。
