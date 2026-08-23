# 飞猪哨兵 · Cookie 同步 Chrome 扩展

> 自动把飞猪 mtop cookies 从浏览器同步到 VPS。**点一下工具栏图标就全自动**——扩展会自己开一个手机尺寸的窗口访问飞猪 H5、自动抓 cookie、自动上传。

## 安装（一次性）

1. Chrome 地址栏打开 `chrome://extensions/`
2. 右上角打开 **开发者模式**
3. 顶部出现 **"加载已解压的扩展程序"** 按钮 → 点击 → 选择 `fliggy-cookie-sync/` 整个目录
4. 安装后 Chrome 工具栏会出现一个橙色 **F** 图标

> ⚠️ 不要从 Chrome 应用商店装；本扩展是内部工具、未经审核分发。

## 使用

### 首次配置（只做一次）

1. 点工具栏的橙色 **F** 图标，弹出配置面板
2. **服务地址**：默认已填 `https://feizhu.19880913.xyz`，直接用即可
3. **同步密钥**：到 dashboard **设置 → Cookie 同步扩展** 卡片里复制（带「📋 复制」按钮），粘贴到输入框
4. 关掉弹出框即可，配置已保存到 `chrome.storage.local`

### 之后每次 cookie 失效时

**点一下工具栏的橙色 F 图标就行**——什么也不用做。

扩展会自动：
1. 开一个 414×896（iPhone 11 手机尺寸）的窗口访问飞猪 H5 商品页（不抢前台、不打扰你）
2. 等页面加载完，再等 4 秒让 cookie 全部落地（包括 JS 二次写入的）
3. 用 `chrome.cookies.getAll` 抓 4 个必需 mtop cookie（HttpOnly 的 `cookie2` 也能拿）
4. POST 到 `${服务地址}/api/cookies/sync`，带 `X-Sync-Secret` 头
5. 关掉那个临时窗口
6. 在弹出框里告诉你结果（成功 / 失败原因）

整个过程大概 6-10 秒。如果弹出框里看到「缺少必需 cookie」，说明浏览器里没登录态——把刚才那个临时窗口切回来登录飞猪账号，然后再点一次图标。

### 关于「手机尺寸窗口」

飞猪 H5 是 mobile-only 的页面，在桌面 Chrome 默认尺寸下经常显示成降级版或者跳到下载 App 引导页。
开一个 414×896（iPhone 11 portrait）的窗口能确保 H5 按手机版正常渲染，登录态和 cookie 都能正常种。

这个窗口是**临时的**，同步完（成功或失败）会自动关掉，不会一直占位置。

## 它做了什么

- 用 `chrome.cookies.getAll({domain: ".taobao.com"})`（以及 `.fliggy.com`/`.alipay.com`/`.tmall.com`）抓当前浏览器的所有 mtop 相关 cookies
- 校验必需 4 个 cookie 都在：`_m_h5_tk`, `_m_h5_tk_enc`, `cookie2`, `t`
- POST 到 `${服务地址}/api/cookies/sync`，带 `X-Sync-Secret` 头
- 服务端写到 `/etc/fliggy-monitor/cookies.json`（chmod 644），覆盖旧值
- 不收集、不外发任何其他 cookie；只携带必需 4 个（外加一些风控字段 `tfstk`/`isg` 等，监控脚本现在不用但保留以备扩展）

## 如何获取"同步密钥"

密钥是 VPS 上 `fliggy-web.service` 的环境变量 `COOKIE_SYNC_SECRET`，**给运营同事复制**：直接登 dashboard → **设置 → Cookie 同步扩展** 卡片，点「📋 复制」一键拿到。

运维自查 / 重新生成：

```bash
ssh root@107.172.144.102 'systemctl show fliggy-web -p Environment --no-pager | grep COOKIE_SYNC_SECRET'
# → Environment=COOKIE_SYNC_SECRET=KHU5tcd0BB-l2IuE4YhOO034D1CUFm9ZSPFMfc9uV6A
```

如果是首次部署还没有密钥，生成一个：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

然后编辑 `/etc/systemd/system/fliggy-web.service`，加一行：

```ini
Environment="COOKIE_SYNC_SECRET=<刚才生成的密钥>"
```

`systemctl daemon-reload && systemctl restart fliggy-web` 即生效。密钥一旦改了，运营同事需要重新到 dashboard 复制并填进扩展。

## 何时需要同步

飞猪 H5 mtop cookies 大致的失效信号：

- VPS 监控日志开始大量 `mtop ret != SUCCESS` 或 `HTTP 401/403`
- dashboard `/alerts` 页面显示 `cookie_refresh_failed` 类告警
- 手动跑 `curl https://feizhu.19880913.xyz/healthz` 仍 OK，但 `/poi/<id>` 详情页长时间不更新

一般 **几天到几周** 一次，具体看运营人员的飞猪账号活跃度。

## 故障排查

| 现象 | 排查 |
|---|---|
| 弹出框显示「缺少必需 cookie: ...」 | 临时窗口里没登录。在那个窗口里登录飞猪账号，再点一次图标 |
| 临时窗口一直停在飞猪 App 下载引导页 | H5 觉得你 UA / viewport 不对——这种不会发生，因为我们就是按 mobile 尺寸开的；偶发时把临时窗口关掉再点一次图标 |
| 弹出框显示「网络请求失败」 | 服务地址写错、或 VPN/防火墙挡了 |
| 弹出框显示「服务端拒绝：invalid or missing X-Sync-Secret」 | 密钥错了、或者 VPS 上 systemd 环境变量没设 |
| 上传成功但监控仍报失败 | cookie 实际拿到了但 mtop 服务端拒了（可能飞猪账号被风控），过 5 分钟再试一次 |
| 弹出框第一次打开就显示「请先填写服务地址和同步密钥」 | 首次使用，按上面"首次配置"做一次 |

## 文件结构

```
fliggy-cookie-sync/
├── manifest.json      # MV3 manifest（v1.1：含 background service_worker）
├── background.js      # 后台 worker：开手机窗口 + 抓 cookie + 上传
├── popup.html         # 弹出窗口 UI（配置 + 状态展示）
├── popup.js           # 弹出框逻辑（触发后台、监听进度）
├── popup.css          # 样式（深色，跟 dashboard 一致）
├── icons/{16,48,128}.png  # 工具栏图标
├── README.md          # 本文件
└── build.sh           # 打包成 dist/fliggy-cookie-sync.zip
```