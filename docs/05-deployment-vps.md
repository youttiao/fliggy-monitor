# 05 · VPS 部署方案（107.172.144.102）

> 目标：在 Ubuntu 22.04 VPS 上把监控 + Web 控制台 + Cookie 续期全部跑起来，对外通过 `https://feizhu.19880913.xyz` 提供服务。本机为客户端，通过 SSH 部署。

---

## 5.1 前置条件确认

| 项 | 值 | 状态 |
|---|---|---|
| 公网 IP | 107.172.144.102 | ✅ |
| SSH | root@107.172.144.102:22 / eX3LcxR852I7jU9Bbn | ✅ |
| 二级域名 | feizhu.19880913.xyz → 107.172.144.102 (A) | 已配置（待 DNS 生效） |
| 操作系统 | 假定 Ubuntu 22.04 LTS（VPS 默认） | 待 SSH 验证 |
| 防火墙 | 需开 22 + 80 + 443 | 待部署 |
| 国内/海外 | 飞猪 mtop 需国内机房——**确认 VPS 是国内机房**（107.x.x.x 不是典型海外段，但需验证） | 待 ping `h5api.m.taobao.com` |

---

## 5.2 部署前 SSH 自检

```bash
# 1. 本机测试连接
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    root@107.172.144.102 'echo OK; uname -a; cat /etc/os-release; df -h /; free -h'
# 期望：
#   - OK
#   - Linux ... 5.x
#   - Ubuntu 22.04.x LTS (or Debian 12)
#   - / 有 ≥ 10G 可用
#   - RAM ≥ 512MB

# 2. 测试到飞猪连通性
ssh root@107.172.144.102 \
    'curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" \
         -H "referer: https://market.m.taobao.com/" \
         "https://h5api.m.taobao.com/h5/mtop.trip.serverless.api.gateway/2.0?type=originaljson&data=%7B%22fcGroup%22%3A%22fl-channel-data%22%2C%22fcName%22%3A%22ticketPoi%22%2C%22fcData%22%3A%7B%22dataType%22%3A%22shelf%22%2C%22poiId%22%3A%221345%22%7D%2C%22source%22%3A%22standard_shelf%22%2C%22pageSource%22%3A%22standard_shelf%22%2C%22h5Version%22%3A%221.0.26%22%7D&ttid=201300%40travel_h5_3.1.0&appKey=12574478&t=1&sign=abc"'
# 期望：HTTP 200 或 403（sign 错但网络通），不能 connect refused

# 3. DNS 验证
dig feizhu.19880913.xyz +short
# 期望：107.172.144.102
```

**任一失败 → 停止部署，先修基础设施。**

---

## 5.3 一次性基础环境

```bash
ssh root@107.172.144.102 << 'EOF'
set -e

# 1. 系统更新
apt update && apt upgrade -y

# 2. 必要软件
apt install -y python3 python3-venv python3-pip \
               curl wget jq vim ufw fail2ban \
               sqlite3 ca-certificates \
               libssl-dev libffi-dev

# 3. Caddy（反向代理 + 自动 HTTPS）
apt install -y debian-keyring debian-archive-keyring
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/deb/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# 4. Playwright 依赖（cookie 续期用）
apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
               libcups2 libxkbcommon0 libxcomposite1 libxdamage1 \
               libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
               libcairo2 libasound2

# 5. 防火墙
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

# 6. 创建专用户
useradd -r -m -d /home/monitor -s /usr/sbin/nologin monitor || true

# 7. 目录结构
mkdir -p /opt/fliggy-monitor/{code,data,docs,web,scripts,tests,deploy,logs,backups}
mkdir -p /etc/fliggy-monitor
chown -R monitor:monitor /opt/fliggy-monitor /var/log/fliggy-monitor 2>/dev/null || true
chmod 750 /etc/fliggy-monitor

# 8. 日志目录
mkdir -p /var/log/fliggy-monitor
chown -R monitor:monitor /var/log/fliggy-monitor

# 9. Caddy 数据目录权限（让 Caddy 能写证书）
mkdir -p /var/lib/caddy/.local/share/caddy
chown -R caddy:caddy /var/lib/caddy

echo "=== base env ready ==="
EOF
```

---

## 5.4 上传项目代码

### 5.4.1 本机打包

```bash
# 在本机
cd /Users/argo/666-XCJ/fliggy-monitor
tar czf /tmp/fliggy-monitor.tar.gz \
    --exclude='*.pyc' --exclude='__pycache__' \
    --exclude='.DS_Store' --exclude='*.db' \
    --exclude='backups/*.db' \
    code/ data/ docs/ web/ scripts/ tests/ deploy/ README.md

ls -lh /tmp/fliggy-monitor.tar.gz
# 期望：< 1MB
```

### 5.4.2 上传并展开

```bash
scp /tmp/fliggy-monitor.tar.gz root@107.172.144.102:/tmp/
ssh root@107.172.144.102 << 'EOF'
cd /opt/fliggy-monitor
tar xzf /tmp/fliggy-monitor.tar.gz
chown -R monitor:monitor /opt/fliggy-monitor
echo "files deployed:"
ls -la
echo "---"
ls code/ web/ data/ scripts/ deploy/
EOF
```

---

## 5.5 注入 Cookies（关键步骤）

```bash
ssh root@107.172.144.102 << 'EOF'
# 在浏览器（mac Chrome 已开 F12）抓：
#   访问 https://outfliggys.m.taobao.com/app/trip/rx-trip-ticket/pages/detail?poiId=1345
#   在 Network 找到 mtop.fliggy.traveldetail.ticket.booktips.new.get 请求
#   右键 → Copy as cURL (bash)
#   从 -H 'cookie: ...' 里抽 4 个 key

# 把内容粘到 /etc/fliggy-monitor/cookies.json（不要进 git！）
cat > /etc/fliggy-monitor/cookies.json << 'JSON'
{
  "_m_h5_tk": "<32-char hex>_<13-digit unix-ms>",
  "_m_h5_tk_enc": "<32-char hex>",
  "cookie2": "<32-char hex>",
  "t": "<32-char hex>"
}
JSON

chmod 600 /etc/fliggy-monitor/cookies.json
chown root:root /etc/fliggy-monitor/cookies.json

# 验证 sign 算法工作（用 cookie 前半段 + 假 data）
python3 /opt/fliggy-monitor/scripts/verify_sign.py
EOF
```

> ⚠️ Cookies 只能通过 SSH 通道传递，禁止 commit 到 git。`data/` 目录里已经准备好 `seller_cache.json`（已含 16 个 seller 元数据），但**不含 cookies**。

---

## 5.6 Python 依赖安装

```bash
ssh root@107.172.144.102 << 'EOF'
# /opt/fliggy-monitor 下的 venv（不需要 sudo，用 monitor 用户）
sudo -u monitor python3 -m venv /opt/fliggy-monitor/.venv
sudo -u monitor /opt/fliggy-monitor/.venv/bin/pip install \
    --upgrade pip \
    fastapi 'uvicorn[standard]' jinja2 \
    python-multipart bcrypt httpx \
    playwright pytest

# Playwright 浏览器（仅 chromium）
sudo -u monitor /opt/fliggy-monitor/.venv/bin/playwright install chromium
EOF
```

> `playwright install chromium` 会下载 ~150MB 到 `~/.cache/ms-playwright`。确保 `/home/monitor` 有 ≥ 500MB。

---

## 5.7 初始化数据库

```bash
ssh root@107.172.144.102 << 'EOF'
sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 \
    /opt/fliggy-monitor/scripts/init_db.py
echo "DB created:"
sqlite3 /opt/fliggy-monitor/data/monitor.db '.tables'
# 期望：alerts cells_snapshot config cookies_history pois rounds sellers
EOF
```

`init_db.py` 会：
1. 建所有表
2. 导入 `data/poi_registry.json` 的 8 POI
3. 导入 `data/seller_cache.json` 的 16 sellers
4. 写入默认 config（包括 `admin_password_hash` = bcrypt("xuran888")）
5. 把 cookies 路径写入 config（不复制 cookies 内容）

---

## 5.8 部署 systemd 单元

```bash
ssh root@107.172.144.102 << 'EOF'
# 上传 unit 文件
cp /opt/fliggy-monitor/deploy/systemd/*.service /etc/systemd/system/
cp /opt/fliggy-monitor/deploy/systemd/*.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now fliggy-web.service
systemctl enable --now fliggy-monitor.service
systemctl enable --now fliggy-cookies-refresh.timer

sleep 2
systemctl status fliggy-web --no-pager -l | head -20
systemctl status fliggy-monitor --no-pager -l | head -20
systemctl list-timers fliggy-cookies-refresh.timer --no-pager

echo "---"
journalctl -u fliggy-web -n 20 --no-pager
echo "---"
journalctl -u fliggy-monitor -n 20 --no-pager
EOF
```

### 5.8.1 单元文件内容

#### `deploy/systemd/fliggy-web.service`

```ini
[Unit]
Description=Fliggy Sentinel Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=monitor
WorkingDirectory=/opt/fliggy-monitor
Environment="PATH=/opt/fliggy-monitor/.venv/bin"
ExecStart=/opt/fliggy-monitor/.venv/bin/uvicorn web.server:app \
    --host 127.0.0.1 --port 8080 --workers 2 --log-level info
Restart=always
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/fliggy-monitor /var/log/fliggy-monitor
ProtectHome=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

#### `deploy/systemd/fliggy-monitor.service`

```ini
[Unit]
Description=Fliggy H5 POI Monitor (scheduled every 30 min)
After=network-online.target

[Service]
Type=oneshot
User=monitor
WorkingDirectory=/opt/fliggy-monitor
Environment="PATH=/opt/fliggy-monitor/.venv/bin"
ExecStart=/opt/fliggy-monitor/.venv/bin/python3 \
    /opt/fliggy-monitor/code/fliggy_monitor.py
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/fliggy-monitor /var/log/fliggy-monitor

[Install]
WantedBy=multi-user.target
```

#### `deploy/systemd/fliggy-monitor.timer`

```ini
[Unit]
Description=Fliggy Monitor runs every 30 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
OnCalendar=*:0/30
Persistent=true
AccuracySec=10s

[Install]
WantedBy=timers.target
```

#### `deploy/systemd/fliggy-cookies-refresh.service`

```ini
[Unit]
Description=Refresh Fliggy cookies via Playwright

[Service]
Type=oneshot
User=monitor
WorkingDirectory=/opt/fliggy-monitor
Environment="PATH=/opt/fliggy-monitor/.venv/bin"
ExecStart=/opt/fliggy-monitor/.venv/bin/python3 \
    /opt/fliggy-monitor/scripts/refresh_cookies.py
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/etc/fliggy-monitor /opt/fliggy-monitor /var/log/fliggy-monitor
EOF
```

> 注意 `ReadWritePaths=/etc/fliggy-monitor`——cookie 续期需要写入 cookies.json。

#### `deploy/systemd/fliggy-cookies-refresh.timer`

```ini
[Unit]
Description=Refresh cookies every 90 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=90min
Persistent=true
AccuracySec=60s

[Install]
WantedBy=timers.target
```

---

## 5.9 Caddyfile（`deploy/Caddyfile`）

```caddyfile
feizhu.19880913.xyz {
    reverse_proxy 127.0.0.1:8080

    encode gzip zstd

    log {
        output file /var/log/fliggy-monitor/access.log {
            roll_size 10mb
            roll_keep 14
        }
    }

    # 安全头
    header {
        # 移除 server 标识
        -Server
        # HSTS
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        # 防止 MIME 嗅探
        X-Content-Type-Options "nosniff"
        # 防点击劫持
        X-Frame-Options "DENY"
        # XSS 保护（现代浏览器已自带）
        X-XSS-Protection "1; mode=block"
        # Referrer 限制
        Referrer-Policy "strict-origin-when-cross-origin"
        # CSP（较严；内部工具够用）
        Content-Security-Policy "default-src 'self'; img-src 'self' data: https://*.alicdn.com https://*.taobao.com https://*.tbcdn.cn; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; font-src 'self' data:"
    }

    # 静态资源缓存
    @static path /static/*
    handle @static {
        header Cache-Control "public, max-age=3600"
    }

    # 健康检查端点
    handle /healthz {
        respond "ok" 200
    }
}
```

部署：

```bash
ssh root@107.172.144.102 << 'EOF'
cp /opt/fliggy-monitor/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl enable --now caddy
sleep 3
systemctl status caddy --no-pager -l | head -15
echo "---"
curl -sS -o /dev/null -w "https: %{http_code}\n" https://feizhu.19880913.xyz/healthz
EOF
```

> 第一次启动 Caddy 会自动向 Let's Encrypt 申请证书（需要 DNS 已生效 + 80/443 可达）。若失败，检查 `journalctl -u caddy`。

---

## 5.10 logrotate（`deploy/logrotate.d-fliggy-monitor`）

```
/var/log/fliggy-monitor/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0644 monitor monitor
    sharedscripts
    postrotate
        systemctl reload fliggy-web || true
    endscript
}
```

部署：

```bash
ssh root@107.172.144.102 << 'EOF'
cp /opt/fliggy-monitor/deploy/logrotate.d-fliggy-monitor \
   /etc/logrotate.d/fliggy-monitor
logrotate -d /etc/logrotate.d/fliggy-monitor  # dry-run 验证
EOF
```

---

## 5.11 冒烟测试

```bash
ssh root@107.172.144.102 << 'EOF'
echo "=== 1. 本地 HTTP ==="
curl -sS http://127.0.0.1:8080/healthz && echo

echo "=== 2. 登录页可访问 ==="
curl -sS -o /dev/null -w "HTTP %{http_code} %{size_download}B %{time_total}s\n" \
    https://feizhu.19880913.xyz/login

echo "=== 3. 监控脚本可拉数据 ==="
sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 \
    /opt/fliggy-monitor/tests/smoke.py 2>&1 | tail -30
# 期望：smoke ok，至少 6 个非自营 cell

echo "=== 4. DB 有数据 ==="
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT COUNT(*) AS sellers FROM sellers;
     SELECT COUNT(*) AS pois FROM pois;
     SELECT key, substr(value,1,30) FROM config LIMIT 5;"

echo "=== 5. systemd 状态 ==="
systemctl is-active fliggy-web fliggy-monitor fliggy-cookies-refresh.timer caddy
EOF
```

---

## 5.12 一键部署脚本（`scripts/deploy.sh`）

```bash
#!/usr/bin/env bash
# 部署到 VPS 的本地入口（本机运行）
set -euo pipefail

VPS="107.172.144.102"
USER="root"
PROJECT_DIR="/Users/argo/666-XCJ/fliggy-monitor"
REMOTE_DIR="/opt/fliggy-monitor"

# 1. 打包
cd "$PROJECT_DIR"
tar czf /tmp/fliggy-monitor.tar.gz \
    --exclude='*.pyc' --exclude='__pycache__' \
    --exclude='.DS_Store' --exclude='*.db' \
    --exclude='backups/*.db' \
    code/ data/ docs/ web/ scripts/ tests/ deploy/ README.md

# 2. 上传
scp /tmp/fliggy-monitor.tar.gz "$USER@$VPS:/tmp/"

# 3. 远程展开 + 重启服务
ssh "$USER@$VPS" << EOF
set -e
mkdir -p $REMOTE_DIR
tar xzf /tmp/fliggy-monitor.tar.gz -C $REMOTE_DIR --strip-components=0
chown -R monitor:monitor $REMOTE_DIR

cd $REMOTE_DIR
sudo -u monitor .venv/bin/python3 scripts/init_db.py || true

systemctl daemon-reload
systemctl restart fliggy-web
systemctl restart fliggy-monitor.timer || true

echo "=== deploy done ==="
systemctl status fliggy-web --no-pager | head -10
EOF
```

`chmod +x scripts/deploy.sh` 后，本机跑：

```bash
./scripts/deploy.sh
```

---

## 5.13 部署后 24 小时监控 checklist

```bash
# 每小时检查一次（连续 24h）
ssh root@107.172.144.102 << 'EOF'
echo "--- $(date) ---"
echo "[timer] fliggy-monitor:"
systemctl list-timers fliggy-monitor.timer --no-pager
echo "[timer] fliggy-cookies-refresh:"
systemctl list-timers fliggy-cookies-refresh.timer --no-pager
echo "[rounds] 最近 10 轮:"
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT round_id, status, cells_total, cells_non_self, duration_ms
     FROM rounds ORDER BY started_at DESC LIMIT 10;"
echo "[alerts] 最近 10 条:"
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT ts, type, severity, webhook_status FROM alerts ORDER BY ts DESC LIMIT 10;"
echo "[errors] 最近 5 条错误:"
journalctl -p err -n 5 --no-pager
EOF
```

观察指标：
- [ ] `fliggy-monitor.timer` 30 min 准时触发（误差 < 1 min）
- [ ] `cells_non_self` 数稳定（不是 0，否则 cookie 失效）
- [ ] `duration_ms` < 30s（每轮）
- [ ] 没有任何 `status=failed` 的 round
- [ ] `alerts` 表有推送记录，且 `webhook_status=sent` 占比 ≥ 95%

---

## 5.14 升级 / 回滚

### 升级

```bash
# 本机
vim code/fliggy_monitor.py  # 改完
./scripts/deploy.sh         # 自动 scp + restart
```

### 回滚

```bash
ssh root@107.172.144.102 << 'EOF'
# 1. 停服务
systemctl stop fliggy-web fliggy-monitor.timer

# 2. 还原代码
cd /opt/fliggy-monitor
# 假设上一个版本在 /tmp/fliggy-monitor.tar.gz.bak
tar xzf /tmp/fliggy-monitor.tar.gz.bak --overwrite

# 3. 重启
systemctl start fliggy-web fliggy-monitor.timer
EOF
```

### DB 回滚

```bash
# 备份目录 /opt/fliggy-monitor/backups/monitor-YYYYMMDD.db
ssh root@107.172.144.102 "\
    systemctl stop fliggy-web && \
    cp /opt/fliggy-monitor/backups/monitor-20260823.db \
       /opt/fliggy-monitor/data/monitor.db && \
    systemctl start fliggy-web"
```

---

## 5.15 故障应急

| 症状 | 可能原因 | 应急 |
|---|---|---|
| 监控 2 小时后停 | cookie 失效 | 检查 `fliggy-cookies-refresh.timer` 是否触发、playwright 是否可用 |
| `shelf_error` 告警 | mtop 接口变化 / 风控 | 抓浏览器最新请求对比 UA / data 参数 |
| Web 502 | uvicorn 死 | `systemctl restart fliggy-web` |
| Webhook 全失败 | URL 错 / token 失效 | 登录 dashboard → 设置 → 测试推送 |
| TLS 证书失败 | Caddy 网络受限 | `journalctl -u caddy` 看 LE 报错；临时改用自签证书 |
| 磁盘满 | DB 膨胀 | 跑 `vacuum`：`sqlite3 monitor.db 'VACUUM;'` |

---

## 5.16 接下来

实现：
- `scripts/deploy.sh`（5.12 的内容）
- `scripts/init_db.py`（建表 + 导入 JSON + bcrypt 密码）
- `scripts/refresh_cookies.py`（playwright 自动续期）
- `deploy/systemd/*`（5 个 unit）
- `deploy/Caddyfile`
- `deploy/logrotate.d-fliggy-monitor`