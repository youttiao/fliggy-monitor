// 飞猪哨兵 Cookie 同步 — popup 逻辑
//
// v1.1：点图标 → 自动触发同步流程（通过 background.js 开小窗 + 抓 cookie + 上传）。
// 首次使用在弹出框填 endpoint + secret，之后完全自动。
//
// 流程：
//   1. 读 chrome.storage.local 里的 endpoint + secret
//   2. 若有 → 立刻发消息给 background 启动 auto_sync；监听 progress/done 更新 UI
//   3. 若缺 → 显示 config 表单，用户填好后点"立即同步"或重新打开弹出框

const STORAGE_KEYS = {
  endpoint: "endpoint",
  secret: "secret",
  lastSync: "lastSync",
  lastCookies: "lastCookies",
};

const REQUIRED = ["_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t"];

const $ = (id) => document.getElementById(id);

const PHASE_LABEL = {
  open: "📱 打开手机窗口",
  reuse: "♻️ 复用已有窗口",
  reload: "🔄 刷新页面",
  grace: "⏸️ 等待 cookie 落地",
  grab: "🍪 抓取 cookies",
  "grab-doc": "🍪 补抓分区 cookie",
  upload: "📤 上传到 VPS",
};

function loadConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(
      [STORAGE_KEYS.endpoint, STORAGE_KEYS.secret, STORAGE_KEYS.lastSync, STORAGE_KEYS.lastCookies],
      (v) => resolve(v || {})
    );
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
    $("cookies-preview").textContent =
      lines.join("\n") +
      (Object.keys(cfg.lastCookies).length > preview.length
        ? `\n… 还有 ${Object.keys(cfg.lastCookies).length - preview.length} 个`
        : "");
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

async function persistConfig() {
  const endpoint = $("endpoint").value.trim().replace(/\/+$/, "");
  const secret = $("secret").value.trim();
  if (!endpoint || !secret) return false;
  await saveConfig({ [STORAGE_KEYS.endpoint]: endpoint, [STORAGE_KEYS.secret]: secret });
  return true;
}

let syncing = false;

async function triggerAutoSync() {
  if (syncing) return;
  syncing = true;
  $("retry").disabled = true;
  try {
    // 每次同步前保存最新输入
    const ok = await persistConfig();
    if (!ok) {
      setStatus("err", "❌ 请先填写服务地址和同步密钥");
      return;
    }
    const cfg = await loadConfig();
    setStatus("warn", "🚀 启动自动同步（开手机窗口 + 抓 cookie + 上传）…");

    // 监听 background 推送的进度
    const onProgress = (msg) => {
      if (msg?.type === "progress") {
        const label = PHASE_LABEL[msg.phase] || msg.phase;
        setStatus("warn", `${label}<br><small>${msg.msg || ""}</small>`);
      } else if (msg?.type === "done") {
        chrome.runtime.onMessage.removeListener(onProgress);
        finishAutoSync(msg.result);
      }
    };
    chrome.runtime.onMessage.addListener(onProgress);

    chrome.runtime.sendMessage({
      type: "auto_sync",
      endpoint: cfg.endpoint,
      secret: cfg.secret,
    });
  } finally {
    // 注意：按钮在 finishAutoSync 里恢复
  }
}

async function finishAutoSync(result) {
  syncing = false;
  $("retry").disabled = false;

  if (result?.ok) {
    const now = Math.floor(Date.now() / 1000);
    const cfg = await loadConfig();
    await saveConfig({ [STORAGE_KEYS.lastSync]: now, [STORAGE_KEYS.lastCookies]: cfg.lastCookies || {} });
    setStatus(
      "ok",
      `✅ 上传成功！写入 <code>${result.path}</code>，共 ${result.saved} 个 cookie。<br>` +
        `等下一轮 monitor 自动跑（≤ 30 分钟）。`
    );
    await refreshMeta();
    await refreshServerMtime();
  } else {
    const err = result?.error || "未知错误";
    setStatus(
      "err",
      `❌ 同步失败：<code>${String(err).replace(/</g, "&lt;")}</code><br>` +
        `在弹出的窗口里登录飞猪账号后点「立即同步」重试`
    );
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await loadConfig();

  // 填表单（首次会显示空 secret）
  if (cfg.endpoint) $("endpoint").value = cfg.endpoint;
  if (cfg.secret) $("secret").value = cfg.secret;

  // 是否已有完整配置 → 切到自动同步模式
  const haveConfig = !!(cfg.endpoint && cfg.secret);
  $("config-section").hidden = haveConfig;
  $("auto-section").hidden = !haveConfig;

  $("retry").addEventListener("click", triggerAutoSync);
  $("save-and-sync").addEventListener("click", triggerAutoSync);

  // 输入变化时清状态 + 切回 config 模式以便用户重填
  const onInput = () => {
    clearStatus();
    $("config-section").hidden = false;
    $("auto-section").hidden = true;
  };
  $("endpoint").addEventListener("input", onInput);
  $("secret").addEventListener("input", onInput);

  // 首次进入（已有配置）自动触发
  if (haveConfig) {
    triggerAutoSync();
  }

  await refreshMeta();
  await refreshServerMtime();
});