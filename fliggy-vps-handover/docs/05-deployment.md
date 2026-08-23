# 05 · VPS 部署步骤

> 最小化、可复现的部署流程。目标：1 台 Ubuntu/Debian VPS，跑监控 + 续期 + 告警。

---

## 1. 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| OS | Ubuntu 22.04 LTS | Debian 12 / Ubuntu 24.04 |
| CPU | 1 vCPU | 2 vCPU |
| RAM | 512 MB | 1 GB |
| Disk | 10 GB | 20 GB（含日志/历史数据） |
| 网络 | 国内机房（阿里云/腾讯云）就行，**不需要海外** | 同左 |
| Python | 3.10+ | 3.12 |
| curl | 7.x | 系统自带 |

⚠️ **不要用海外 VPS**（AWS 美西 / Vultr / DO）—— 飞猪 H5 mtop 在海外 IP 会被风控降级，部分接口返回空数据。

---

## 2. 用户与目录

```bash
# 创建专用户
sudo useradd -r -m -d /home/fliggy -s /bin/bash fliggy
sudo mkdir -p /etc/fliggy-vps /var/log/fliggy-vps /opt/fliggy-vps
sudo chown -R fliggy:fliggy /var/log/fliggy-vps /opt/fliggy-vps
sudo chmod 700 /etc/fliggy-vps  # cookies 敏感文件
```

---

## 3. 部署代码

```bash
# 把包 scp 到 VPS
scp -r handover/fliggy-vps/  fliggy@<vps>:/opt/fliggy-vps/

# 在 VPS 上
cd /opt/fliggy-vps
ls -la
# 应该看到: README.md docs data code tests
```

### 3.1 设置 Python 环境（零依赖）

```bash
# 用系统 Python；零三方依赖
python3 --version  # 3.10+
which curl        # /usr/bin/curl

# 不需要 pip install 任何东西
```

### 3.2 注入 cookies（最关键）

```bash
sudo vim /etc/fliggy-vps/cookies.json
```

格式：

```json
{
  "_m_h5_tk": "75e43700f8abc0a74c65078d898c5c18_1787466255406",
  "_m_h5_tk_enc": "0148e457b4d114c9241c5d25ef3360ee",
  "cookie2": "198a5686620872d3855b9884d702c2cc",
  "t": "efa270ed8840101a88a935a9f0f5a6fe"
}
```

⚠️ **chmod 600 + owner root**，避免泄露：

```bash
sudo chmod 600 /etc/fliggy-vps/cookies.json
sudo chown root:root /etc/fliggy-vps/cookies.json
```

详见 [`03-cookies-and-sign.md`](03-cookies-and-sign.md)。

### 3.3 冒烟测试

```bash
cd /opt/fliggy-vps
sudo -u fliggy python3 tests/smoke.py
# 预期：6 个非自营 cell，sellerName 拿到，✓ smoke ok
```

如果报错 `HTTP 403` / `FAIL_SYS_SESSION_EXPIRED`，**cookies 失效**了，重新从浏览器抓。

---

## 4. Cookie 续期（核心运维）

`_m_h5_tk` 约 2 小时滑动窗。**必须**实现自动续期，否则监控跑 2 小时就停。

### 方案 A：playwright headless（推荐）

```bash
# 一次性安装
pip install playwright  # 全局 or 在 venv
playwright install chromium
```

脚本 `tools/refresh_cookies.py`（新项目自己写）：

```python
"""每 90 min 用 playwright 跑淘宝 H5 拿新 cookies。"""
import asyncio, json
from playwright.async_api import async_playwright

async def refresh():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        # 访问任意飞猪 POI 详情页，触发 mtop 调用 → 拿到 cookies
        await page.goto("https://outfliggys.m.taobao.com/app/trip/rx-trip-ticket/pages/detail?poiId=1345")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(5)  # 等 mtop 响应回来
        
        cookies = await ctx.cookies()
        out = {c["name"]: c["value"] for c in cookies 
               if c["name"] in ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "t")}
        
        with open("/etc/fliggy-vps/cookies.json", "w") as f:
            json.dump(out, f, indent=2)
        
        await browser.close()
        return out

if __name__ == "__main__":
    asyncio.run(refresh())
```

加 systemd timer 每 90 min 跑一次（见下面 5.3）。

### 方案 B：mitm + 真机（如果方案 A 拿不到）

用 mac + 真机 + mitmproxy 走 WireGuard 模式抓，详见老项目 `docs/network-capture.md`。

### 方案 C：手工续期（最差，临时用）

```bash
# 临时方案：cron 提醒人工续期
*/30 * * * *  echo "Check cookies" | mail -s "Fliggy cookies refresh needed" you@example.com
```

---

## 5. systemd 服务

### 5.1 监控主循环

`/etc/systemd/system/fliggy-monitor.service`：

```ini
[Unit]
Description=Fliggy H5 POI Monitor
After=network-online.target

[Service]
Type=simple
User=fliggy
WorkingDirectory=/opt/fliggy-vps
ExecStart=/usr/bin/python3 /opt/fliggy-vps/code/fliggy_monitor.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/log/fliggy-vps /opt/fliggy-vps

[Install]
WantedBy=multi-user.target
```

### 5.2 Cookie 续期服务

`/etc/systemd/system/fliggy-cookies-refresh.service`：

```ini
[Unit]
Description=Refresh Fliggy cookies via playwright

[Service]
Type=oneshot
User=fliggy
ExecStart=/usr/bin/python3 /opt/fliggy-vps/tools/refresh_cookies.py
WorkingDirectory=/opt/fliggy-vps
```

### 5.3 续期定时器

`/etc/systemd/system/fliggy-cookies-refresh.timer`：

```ini
[Unit]
Description=Refresh Fliggy cookies every 90 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=90min
Persistent=true

[Install]
WantedBy=timers.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fliggy-monitor.service
sudo systemctl enable --now fliggy-cookies-refresh.timer

sudo systemctl status fliggy-monitor
sudo systemctl list-timers fliggy-cookies-refresh.timer
```

---

## 6. 日志 + 监控

```bash
# 实时日志
sudo journalctl -u fliggy-monitor -f

# 历史日志（每天轮转）
sudo vim /etc/logrotate.d/fliggy-vps
```

`/etc/logrotate.d/fliggy-vps`：

```
/var/log/fliggy-vps/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0644 fliggy fliggy
}
```

---

## 7. 告警通道（新项目里实现）

监控脚本模板里只 dump 到 stdout。新项目改成：

- **钉钉 webhook**：1 个机器人 token，POST JSON，message 含 markdown
- **企微 webhook**：同钉钉，URL 不同
- **Slack**：用 webhook，字段略有差异
- **Email**：用 `smtplib`，配 SendGrid / Mailgun / 阿里云 DM
- **PagerDuty**：用 events API v2，紧急级别

### 推荐告警规则

| 触发条件 | 级别 | 频率 |
|---|---|---|
| cookie 续期失败 | 高 | 实时 |
| shelf 连续 3 轮 5xx | 高 | 实时 |
| 新 sellerId 出现（非自营） | 中 | 实时 |
| 价格 ±20% 异动 | 中 | 每轮 |
| 库存从 0 → >0 / 从 >0 → 0 | 低 | 每轮 |

---

## 8. 数据落盘（推荐）

```bash
# 历史数据目录
sudo mkdir -p /opt/fliggy-vps/data/historical/{shelf,booktips}
sudo chown -R fliggy:fliggy /opt/fliggy-vps/data
```

每个 cell 一份 JSON，按 `(poiId, itemId, skuId, timestamp)` 命名：

```
/opt/fliggy-vps/data/historical/shelf/1345/2026-08-23T12:00:00.json
/opt/fliggy-vps/data/historical/shelf/1345/2026-08-23T12:30:00.json
...
```

便于回溯 / 趋势分析。

---

## 9. 升级 / 维护

```bash
# 抓新 POI（加到 poi_registry.json）
vim /opt/fliggy-vps/data/poi_registry.json
sudo systemctl restart fliggy-monitor

# seller 缓存增量更新
# 用 browser 抓新 seller 的 booktips 响应，加到 seller_cache.json
# 重启服务
sudo systemctl restart fliggy-monitor

# 升级 mtop_client.py
scp code/mtop_client.py fliggy@<vps>:/opt/fliggy-vps/code/
sudo systemctl restart fliggy-monitor
```

---

## 10. 完整 checklist

- [ ] VPS 系统 + Python 3.10+
- [ ] `fliggy` 用户 + 目录权限
- [ ] 代码 scp 到 `/opt/fliggy-vps`
- [ ] `/etc/fliggy-vps/cookies.json` 注入并 chmod 600
- [ ] `python3 tests/smoke.py` 跑通
- [ ] `tools/refresh_cookies.py` 写好
- [ ] systemd service + timer 配好
- [ ] 告警通道 webhook 配好
- [ ] 日志轮转配好
- [ ] 数据落盘目录建好
- [ ] systemd enable + 启动 + journalctl 看 1 小时

跑完这套就是 production ready 了。