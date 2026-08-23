// 飞猪哨兵 Cookie 同步 — popup 逻辑
//
// 流程：
//   1. 读 chrome.storage.local 里的 endpoint + secret
//   2. 用 chrome.cookies.getAll 抓 4 个必需 cookie（HttpOnly 也能拿）
//   3. POST 到 `${endpoint}/api/cookies/sync`，带 X-Sync-Secret 头
//   4. 显示结果

const REQUIRED = ["_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t"];
const STORAGE_KEYS = { endpoint: "endpoint", secret: "secret", lastSync: "lastSync", lastCookies: "lastCookies" };

const $ = (id) => document.getElementById(id);

async function loadConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get([STORAGE_KEYS.endpoint, STORAGE_KEYS.secret, STORAGE_KEYS.lastSync, STORAGE_KEYS.lastCookies], (v) => {
      resolve(v || {});
    });
  });
}

function saveConfig(patch) {
  return new Promise((resolve) => chrome.storage.local.set(patch, () => resolve()));
}

function setStatus(kind, html) {
  const el = $("status");
  el.hidden = false;
  el.className = "status " + (kind || "");
  el.innerHTML = html;
}

function clearStatus() {
  $("status").hidden = true;
  $("status").innerHTML = "";
  $("status").className = "status";
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 抓所有可能的 mtop cookie 域
const COOKIE_DOMAINS = [
  ".taobao.com",
  ".m.taobao.com",
  "taobao.com",
  ".fliggy.com",
  ".alipay.com",
  ".tmall.com",
];

async function grabCookies() {
  const all = {};
  const seen = new Set();
  for (const domain of COOKIE_DOMAINS) {
    let list = [];
    try {
      list = await chrome.cookies.getAll({ domain });
    } catch (e) {
      // 某些域无权限时跳错；忽略
      continue;
    }
    for (const c of list) {
      const key = c.name + "@" + (c.domain || domain);
      if (seen.has(key)) continue;
      seen.add(key);
      // chrome.cookies 返回的 domain 以 "." 开头表示可用子域；value 即原始值
      all[c.name] = c.value;
    }
  }
  return all;
}

async function syncNow() {
  const endpoint = $("endpoint").value.trim().replace(/\/+$/, "");
  const secret = $("secret").value.trim();
  if (!endpoint) {
    setStatus("err", "❌ 请先填写服务地址");
    $("endpoint").focus();
    return false;
  }
  if (!secret) {
    setStatus("err", "❌ 请先填写同步密钥");
    $("secret").focus();
    return false;
  }
  await saveConfig({ [STORAGE_KEYS.endpoint]: endpoint, [STORAGE_KEYS.secret]: secret });

  setStatus("warn", "⏳ 正在从浏览器抓 cookies…");
  const cookies = await grabCookies();

  const missing = REQUIRED.filter((k) => !cookies[k]);
  if (missing.length) {
    setStatus(
      "err",
      `❌ 缺少必需 cookie: <code>${missing.join(", ")}</code><br />` +
        `打开任意飞猪 H5 商品页（如 <a href="https://market.m.taobao.com" target="_blank">market.m.taobao.com</a>）登录后再试。`
    );
    return false;
  }

  setStatus("warn", "⏳ 已抓到 4 个必需 cookie，上传到 VPS…");
  let resp, body;
  try {
    resp = await fetch(`${endpoint}/api/cookies/sync`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Sync-Secret": secret,
      },
      body: JSON.stringify({ cookies }),
    });
  } catch (e) {
    setStatus("err", `❌ 网络请求失败：<code>${String(e).replace(/</g, "&lt;")}</code><br />检查服务地址或网络连通性`);
    return false;
  }

  try {
    body = await resp.json();
  } catch {
    body = { ok: false, error: `HTTP ${resp.status} 但响应不是 JSON` };
  }

  if (!resp.ok || !body.ok) {
    const err = body.error || `HTTP ${resp.status}`;
    setStatus("err", `❌ 服务端拒绝：<code>${String(err).replace(/</g, "&lt;")}</code>`);
    return false;
  }

  const now = Math.floor(Date.now() / 1000);
  await saveConfig({
    [STORAGE_KEYS.lastSync]: now,
    [STORAGE_KEYS.lastCookies]: cookies,
  });
  setStatus(
    "ok",
    `✅ 上传成功！写入 <code>${body.path}</code>，共 ${body.saved} 个 cookie。<br />` +
      `现在可以关闭本扩展，等下一轮 monitor 自动跑（≤ 30 分钟）。`
  );
  refreshMeta();
  return true;
}

async function refreshMeta() {
  const cfg = await loadConfig();
  $("last-sync").textContent = fmtTime(cfg.lastSync);
  if (cfg.lastCookies) {
    const preview = REQUIRED.concat(Object.keys(cfg.lastCookies).filter((k) => !REQUIRED.includes(k))).slice(0, 12);
    const lines = preview.map((k) => {
      const v = cfg.lastCookies[k];
      if (!v) return `${k}: <missing>`;
      const masked = v.length > 8 ? v.slice(0, 4) + "…" + v.slice(-4) : v;
      return `${k.padEnd(18)} = ${masked}  (len=${v.length})`;
    });
    $("cookies-preview").textContent = lines.join("\n") + (Object.keys(cfg.lastCookies).length > preview.length ? `\n… 还有 ${Object.keys(cfg.lastCookies).length - preview.length} 个` : "");
  } else {
    $("cookies-preview").textContent = "— 还没有同步过 —";
  }
}

async function refreshServerMtime() {
  const cfg = await loadConfig();
  const endpoint = (cfg.endpoint || $("endpoint").value).trim().replace(/\/+$/, "");
  const secret = cfg.secret || $("secret").value;
  if (!endpoint || !secret) {
    $("server-mtime").textContent = "—";
    return;
  }
  try {
    const r = await fetch(`${endpoint}/api/cookies/health`, {
      headers: { "X-Sync-Secret": secret },
    });
    if (!r.ok) {
      $("server-mtime").textContent = "(查询失败)";
      return;
    }
    const j = await r.json();
    $("server-mtime").textContent = j.exists ? fmtTime(j.mtime) : "(文件不存在)";
  } catch {
    $("server-mtime").textContent = "(连接失败)";
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await loadConfig();
  if (cfg.endpoint) $("endpoint").value = cfg.endpoint;
  if (cfg.secret) $("secret").value = cfg.secret;

  $("sync").addEventListener("click", async () => {
    $("sync").disabled = true;
    try {
      await syncNow();
    } finally {
      $("sync").disabled = false;
    }
  });

  // endpoint / secret 输入变化时清状态
  $("endpoint").addEventListener("input", () => { clearStatus(); });
  $("secret").addEventListener("input", () => { clearStatus(); });

  await refreshMeta();
  await refreshServerMtime();
});