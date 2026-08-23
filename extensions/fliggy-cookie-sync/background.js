// 飞猪哨兵 Cookie 同步 — 后台 Service Worker
//
// 职责：被 popup 触发 → 开一个手机尺寸的窗口访问飞猪 H5 → 等页面就绪 +
// 给登录跳转留几秒 → 抓 cookies → POST 到 VPS → 关窗口 → 把结果回报给 popup。
//
// 设计点：
//   - 后台跑、popup 关掉也不影响
//   - mobile window 414×896（iPhone 11 portrait），H5 页面会按手机版渲染
//   - 不打扰用户：focused:false（不抢前台），sync 完成后关窗
//   - cookies 抓取走 chrome.cookies API（HttpOnly 的 cookie2 也能拿）

const REQUIRED = ["_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t"];

// 飞猪 H5 商品详情页（标准货架接口所在的页面，能拿到登录态下的全部 mtop cookie）
const H5_URL =
  "https://market.m.taobao.com/app/trip/rx-trip-ticket/pages/detail/index.html?poiId=1345";

const COOKIE_DOMAINS = [
  ".taobao.com",
  ".m.taobao.com",
  "taobao.com",
  ".fliggy.com",
  ".alipay.com",
  ".tmall.com",
];

// mobile window size（Chrome 最小 ~250×250；414×896 是 iPhone 11 portrait，H5 一般适配到这个尺寸）
const MOBILE_W = 414;
const MOBILE_H = 896;

// page-load grace：H5 经常有异步跳转 + cookie 二次写入，给 4 秒缓冲
const GRACE_MS = 4000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function grabAllCookies() {
  const all = {};
  const seen = new Set();
  for (const domain of COOKIE_DOMAINS) {
    let list = [];
    try {
      list = await chrome.cookies.getAll({ domain });
    } catch (e) {
      continue;
    }
    for (const c of list) {
      const key = c.name + "@" + (c.domain || domain);
      if (seen.has(key)) continue;
      seen.add(key);
      all[c.name] = c.value;
    }
  }
  return all;
}

function waitForTabComplete(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };

    const onUpdated = (id, info, tab) => {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(onUpdated);
        clearTimeout(timer);
        finish("complete");
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);

    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      finish("timeout");
    }, timeoutMs);
  });
}

async function postCookies(endpoint, secret, cookies) {
  const resp = await fetch(`${endpoint}/api/cookies/sync`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Sync-Secret": secret,
    },
    body: JSON.stringify({ cookies }),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = { ok: false, error: `HTTP ${resp.status} 但响应不是 JSON` };
  }
  return { status: resp.status, body };
}

async function autoSync({ endpoint, secret }) {
  const log = (phase, msg) =>
    chrome.runtime.sendMessage({ type: "progress", phase, msg }).catch(() => {});
  const finish = (result) =>
    chrome.runtime.sendMessage({ type: "done", result }).catch(() => {});

  if (!endpoint || !secret) {
    finish({ ok: false, error: "缺少 endpoint 或 secret，先在弹出框填一下" });
    return;
  }

  let win = null;
  try {
    log("open", "打开手机尺寸窗口访问飞猪 H5…");
    win = await chrome.windows.create({
      url: H5_URL,
      width: MOBILE_W,
      height: MOBILE_H,
      focused: false,
      type: "normal",
    });
    const tabId = win.tabs?.[0]?.id;
    if (!tabId) throw new Error("打开窗口后没拿到 tabId");

    log("load", "等待页面加载…");
    await waitForTabComplete(tabId);

    log("grace", `页面已就绪，等待 ${GRACE_MS / 1000}s 让 cookie 落地…`);
    await sleep(GRACE_MS);

    log("grab", "抓取浏览器 cookies…");
    const cookies = await grabAllCookies();
    const missing = REQUIRED.filter((k) => !cookies[k]);
    if (missing.length) {
      finish({
        ok: false,
        error:
          `缺少必需 cookie: ${missing.join(", ")}。<br>` +
          `请在刚才弹出的窗口里登录飞猪账号，然后点「重试」`,
        cookiesCount: Object.keys(cookies).length,
      });
      return;
    }

    log("upload", `已抓到 ${Object.keys(cookies).length} 个 cookie，上传到 VPS…`);
    const { status, body } = await postCookies(endpoint, secret, cookies);
    if (!body.ok) {
      finish({
        ok: false,
        error: `服务端拒绝：${body.error || `HTTP ${status}`}`,
        status,
      });
      return;
    }

    finish({
      ok: true,
      path: body.path,
      saved: body.saved,
      status,
    });
  } catch (e) {
    finish({ ok: false, error: String(e?.message || e) });
  } finally {
    if (win?.id != null) {
      try {
        await chrome.windows.remove(win.id);
      } catch {}
    }
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "auto_sync") {
    autoSync(msg).then(() => sendResponse({ started: true }));
    return true; // async
  }
});

// 安装 / 更新时不做任何事（popup 触发即可）。保留 alarms 给将来加定期同步用。
chrome.runtime.onInstalled.addListener(() => {
  // no-op
});