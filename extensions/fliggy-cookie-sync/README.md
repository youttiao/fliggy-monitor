# 飞猪哨兵 · Cookie 同步 Chrome 扩展

> 给运营同事用：当飞猪 mtop cookies 失效（监控开始报 `mtop ret != SUCCESS`）时，打开 Chrome 任意飞猪 H5 页面，点本扩展 → 抓 cookies → 一键上传到 VPS。

## 安装（一次性）

1. Chrome 地址栏打开 `chrome://extensions/`
2. 右上角打开 **开发者模式**
3. 顶部出现 **"加载已解压的扩展程序"** 按钮 → 点击 → 选择 `fliggy-cookie-sync/` 整个目录
4. 安装后 Chrome 工具栏会出现一个橙色 **F** 图标

> ⚠️ 不要从 Chrome 应用商店装；本扩展是内部工具、未经审核分发。

## 使用（每次 cookie 失效时）

1. 在 Chrome 打开任意飞猪 H5 页面，确认自己**已登录**（如 https://market.m.taobao.com/app/trip/rx-trip-ticket/pages/detail）
2. 点工具栏的橙色 **F** 图标
3. 首次使用：
   - **服务地址**：填 `https://feizhu.19880913.xyz`（运维给的）
   - **同步密钥**：填运维给的 `X-Sync-Secret`（一次性输入，存在本地 `chrome.storage.local`，不同步到云）
4. 点 **"从当前浏览器抓取并上传"**
5. 看到 **"✅ 上传成功"** 就关掉扩展，等下一轮监控自动跑（≤ 30 分钟）

## 它做了什么

- 用 `chrome.cookies.getAll({domain: ".taobao.com"})` 抓当前浏览器的所有 mtop 相关 cookies（包括 HttpOnly 的 `cookie2`）
- 校验必需 4 个 cookie 都在：`_m_h5_tk`, `_m_h5_tk_enc`, `cookie2`, `t`
- POST 到 `${服务地址}/api/cookies/sync`，带 `X-Sync-Secret` 头
- 服务端写到 `/etc/fliggy-monitor/cookies.json`（chmod 644），覆盖旧值
- 不收集、不外发任何其他 cookie；只携带必需 4 个（外加一些风控字段 `tfstk`/`isg` 等，监控脚本现在不用但保留以备扩展）

## 如何获取"同步密钥"

密钥是 VPS 上 `fliggy-web.service` 的环境变量 `COOKIE_SYNC_SECRET`：

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

`systemctl daemon-reload && systemctl restart fliggy-web` 即生效。

## 何时需要同步

飞猪 H5 mtop cookies 大致的失效信号：

- VPS 监控日志开始大量 `mtop ret != SUCCESS` 或 `HTTP 401/403`
- dashboard `/alerts` 页面显示 `cookie_refresh_failed` 类告警
- 手动跑 `curl https://feizhu.19880913.xyz/healthz` 仍 OK，但 `/poi/<id>` 详情页长时间不更新

一般 **几天到几周** 一次，具体看运营人员的飞猪账号活跃度。

## 故障排查

| 现象 | 排查 |
|---|---|
| "缺少必需 cookie: ..." | 没在飞猪页面里点过、或者没登录。打开任意飞猪 H5 详情页等几秒再点同步 |
| "网络请求失败" | 服务地址写错、或 VPN/防火墙挡了 |
| "服务端拒绝：invalid or missing X-Sync-Secret" | 密钥错了、或者 VPS 上 systemd 环境变量没设 |
| "服务端拒绝：auth" | 用 `X-Sync-Secret` 之外的头名也会被拒 |
| 上传成功但监控仍报失败 | cookie 实际拿到了但 mtop 服务端拒了（可能飞猪账号被风控），过 5 分钟再试一次 |

## 文件结构

```
fliggy-cookie-sync/
├── manifest.json      # MV3 manifest
├── popup.html         # 弹出窗口 UI
├── popup.css          # 样式（深色，跟 dashboard 一致）
├── popup.js           # 抓 cookie + 上传逻辑
├── icons/{16,48,128}.png  # 工具栏图标
├── README.md          # 本文件
└── build.sh           # 打包成 dist/fliggy-cookie-sync.zip
```