/**
 * 直播监控 - Cloudflare Worker 定时触发器
 * 每10分钟触发一次 GitHub Action
 *
 * 安全：fetch handler 校验 Authorization: Bearer $WORKER_SECRET（若已配置）。
 * 未配置 WORKER_SECRET 时放行并打印警告（向后兼容）。
 */
const GH_OWNER = "racheko-lab";
const GH_REPO = "blive-monitor";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
  };
}

async function dispatchAction(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/workflows/check.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
      },
      body: JSON.stringify({ ref: "master" }),
    }
  );
  return `Triggered: ${resp.status}`;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      dispatchAction(env).catch((e) =>
        console.error("scheduled dispatch failed:", e)
      )
    );
  },

  async fetch(request, env) {
    // OPTIONS 预检
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    // 鉴权
    if (env.WORKER_SECRET) {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.WORKER_SECRET}`) {
        return new Response("Unauthorized", {
          status: 401,
          headers: corsHeaders(),
        });
      }
    } else {
      console.warn("WORKER_SECRET 未配置，fetch handler 无鉴权（向后兼容）");
    }

    try {
      const msg = await dispatchAction(env);
      return new Response(JSON.stringify({ ok: true, message: msg }), {
        headers: { "Content-Type": "application/json", ...corsHeaders() },
      });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: String(e) }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...corsHeaders() },
      });
    }
  },
};
