# 07 · 运维手册（Runbook）

> 日常操作 / 故障排查 / 应急 / 备份 / 升级的速查表。

---

## 7.1 登录

- 地址：`https://feizhu.19880913.xyz/login`
- 密码：`xuran888`（首次部署时由 `init_db.py` 用 bcrypt 写入 config 表）
- 失败 5 次 → 锁 10 分钟（IP 维度）

**修改密码**：登录后 → 设置 → 「登录」区块 → 旧/新/新重复 → 更新

**重置密码**（忘记时，从 VPS 终端）：

```bash
ssh root@107.172.144.102 << 'EOF'
sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 << 'PY'
import sqlite3, bcrypt
db = sqlite3.connect('/opt/fliggy-monitor/data/monitor.db')
hashed = bcrypt.hashpw(b"xuran888", bcrypt.gensalt(rounds=12)).decode()
import json, datetime
db.execute("INSERT OR REPLACE INTO config(key, value, updated_at) VALUES (?, ?, ?)",
    ("admin_password_hash", json.dumps(hashed), datetime.datetime.utcnow().isoformat()))
db.commit()
print("password reset to xuran888")
PY
EOF
```

---

## 7.2 日常巡检（5 min）

```bash
ssh root@107.172.144.102 << 'EOF'
echo "═══ $(date '+%Y-%m-%d %H:%M:%S %Z') ═══"
echo
echo "[1] systemd 状态"
for svc in fliggy-web fliggy-monitor.timer fliggy-cookies-refresh.timer caddy; do
    state=$(systemctl is-active $svc 2>&1)
    echo "  $svc: $state"
done

echo
echo "[2] 最近 5 轮扫描"
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT substr(round_id,2) AS round, status, cells_non_self||'/'||cells_total AS ns, duration_ms||'ms' AS d
     FROM rounds ORDER BY started_at DESC LIMIT 5;"

echo
echo "[3] 最近 5 条 webhook"
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT substr(ts,12,8) AS t, type, severity, webhook_status
     FROM alerts ORDER BY ts DESC LIMIT 5;"

echo
echo "[4] cookie 续期"
sqlite3 /opt/fliggy-monitor/data/monitor.db \
    "SELECT substr(ts,1,19) AS ts, success, error_msg
     FROM cookies_history ORDER BY ts DESC LIMIT 3;"

echo
echo "[5] 磁盘"
df -h /opt/fliggy-monitor /var/log/fliggy-monitor | tail -2
EOF
```

预期输出（健康时）：
- `fliggy-web: active` / `fliggy-monitor.timer: active` / `fliggy-cookies-refresh.timer: active` / `caddy: active`
- 最近 5 轮 `status=success`，`cells_non_self > 0`（说明 cookie 正常）
- `webhook_status=sent`
- cookie 续期 90 min 内有 `success=1`

---

## 7.3 常见问题速查

### Q1. dashboard 502 / 不可达

```bash
ssh root@107.172.144.102
systemctl status fliggy-web
journalctl -u fliggy-web -n 50 --no-pager
# 大概率 uvicorn 进程死了
systemctl restart fliggy-web
curl -sS http://127.0.0.1:8080/healthz
```

### Q2. dashboard 登录失败 / 锁了

```bash
# 清掉锁定
ssh root@107.172.144.102 \
    "sqlite3 /opt/fliggy-monitor/data/monitor.db \
        \"UPDATE config SET value='null', updated_at=datetime('now') \
          WHERE key='login_locked_until';
          UPDATE config SET value='0', updated_at=datetime('now') \
          WHERE key='login_attempts';\""
```

### Q3. 监控轮全 status=failed

```bash
# 看错误
ssh root@107.172.144.102 \
    "sqlite3 /opt/fliggy-monitor/data/monitor.db \
        'SELECT round_id, error_msg FROM rounds WHERE status=\"failed\" ORDER BY started_at DESC LIMIT 3;'"

# 手动跑一次看 stack trace
ssh root@107.172.144.102 \
    'sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 \
     /opt/fliggy-monitor/code/fliggy_monitor.py 2>&1 | tail -30'
```

常见原因：
- cookie 过期 → 跑 `scripts/refresh_cookies.py`
- 飞猪接口变化 → 比对 `code/selectors.py` 常量
- VPS 网络封禁 → 临时切到国内备用 IP

### Q4. cookie 续期持续失败

```bash
ssh root@107.172.144.102 \
    'sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 \
     /opt/fliggy-monitor/scripts/refresh_cookies.py 2>&1 | tail -30'
# 看 stack trace；常见：
#   - playwright chromium 没装好 → 重跑 playwright install chromium
#   - 磁盘满 → 清理
#   - 时间不同步 → chrony/nptdate
```

### Q5. webhook 推送全失败

登录 dashboard → `/settings` → 「测试推送」按钮 → 看响应码：
- 401/403：签名错或 token 过期 → 重新生成 webhook URL
- 404：URL 错
- 429：被限流（钉钉 100 条/min）→ 调整发送频率
- 5xx：接收方故障

如果接收方修了想重发历史失败告警：

```sql
UPDATE alerts SET webhook_status='pending', webhook_retry=0
WHERE webhook_status='failed'
  AND ts > datetime('now', '-1 day');
```
（监控下次扫到会自动重推；或手动 `SELECT id FROM alerts WHERE webhook_status='pending'`）

### Q6. 误报太多（不想收到）

登录 dashboard → 设置 → 关掉对应告警规则。

### Q7. 想监控新 POI

```bash
ssh root@107.172.144.102
sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 << 'PY'
import sqlite3, datetime, json
db = sqlite3.connect('/opt/fliggy-monitor/data/monitor.db')
db.execute("INSERT OR REPLACE INTO pois (poi_id, name, tb_cn, h5_url, enabled, polling_sec, created_at) \
            VALUES (?, ?, ?, ?, 1, 1800, ?)",
            ("<新POI_ID>", "<名称>", "<短链>", "<H5 URL>", datetime.datetime.utcnow().isoformat()))
db.commit()
print("added; restart fliggy-monitor.timer to pick up")
PY
systemctl restart fliggy-monitor.timer
```

### Q8. 误报（想忽略某个卖家）

```sql
INSERT INTO config(key, value, updated_at) VALUES
    ('ignored_seller_ids', json_array('2217xxxxxx', '2217yyyyyy'), datetime('now'));
```

代码读这字段，diff 时排除。

---

## 7.4 备份与恢复

### 自动备份（每日）

在 `monitor` 用户 cron 里加（不用 root cron）：

```bash
# /etc/cron.d/fliggy-monitor-backup (部署脚本自动写)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/opt/fliggy-monitor/.venv/bin

# 每天 03:30 备份 DB + 配置（不含 cookies）
30 3 * * * monitor \
    /opt/fliggy-monitor/.venv/bin/python3 -c "
import shutil, datetime, pathlib
src = pathlib.Path('/opt/fliggy-monitor/data/monitor.db')
dst_dir = pathlib.Path('/opt/fliggy-monitor/backups')
dst_dir.mkdir(exist_ok=True)
ts = datetime.datetime.now().strftime('%Y%m%d')
shutil.copy(src, dst_dir / f'monitor-{ts}.db')
# 保留最近 30 天
import os
files = sorted(dst_dir.glob('monitor-*.db'))
for f in files[:-30]:
    f.unlink()
print('backup done')
" >> /var/log/fliggy-monitor/backup.log 2>&1
```

### 手动备份

```bash
ssh root@107.172.144.102 \
    'sqlite3 /opt/fliggy-monitor/data/monitor.db ".backup \
        /opt/fliggy-monitor/backups/manual-$(date +%Y%m%d-%H%M).db"'
```

### 跨机备份

```bash
# 本机拉
scp root@107.172.144.102:/opt/fliggy-monitor/backups/monitor-*.db \
    ~/backups/fliggy-monitor/
```

### 恢复

```bash
ssh root@107.172.144.102 << 'EOF'
systemctl stop fliggy-web
cp /opt/fliggy-monitor/backups/monitor-20260823.db \
   /opt/fliggy-monitor/data/monitor.db
chown monitor:monitor /opt/fliggy-monitor/data/monitor.db
systemctl start fliggy-web
EOF
```

---

## 7.5 升级

### 应用代码升级

```bash
# 本机
cd /Users/argo/666-XCJ/fliggy-monitor
# 改完代码
./scripts/deploy.sh
```

### 数据库 schema 升级

```bash
ssh root@107.172.144.102 \
    'sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 \
     /opt/fliggy-monitor/scripts/migrate_db.py --to v2'
```

### 依赖升级

```bash
ssh root@107.172.144.102 \
    'sudo -u monitor /opt/fliggy-monitor/.venv/bin/pip install \
     --upgrade fastapi uvicorn playwright httpx bcrypt'
```

---

## 7.6 性能 / 容量参考

| 指标 | 当前 | 一年后预估 | 触发动作 |
|---|---|---|---|
| DB 大小 | ~10 MB | ~80 MB | 启动 daily vacuum |
| 日志大小 | ~5 MB/天 | ~1.8 GB/年 | logrotate 已配（保留 14 天） |
| CPU | < 5% 平均 | < 5% | 无需动作 |
| RAM | ~150 MB | ~200 MB | 1 GB VPS 足够 |
| 磁盘 | ~50 MB | ~2 GB | 20 GB 够用 |

---

## 7.7 安全 checklist（季度）

- [ ] 系统补丁：`apt update && apt upgrade -y`
- [ ] fail2ban 状态：`fail2ban-client status sshd`
- [ ] ufw 状态：`ufw status`
- [ ] SSL 证书有效期（自动续，但可检查）：`curl -vI https://feizhu.19880913.xyz 2>&1 | grep -i expire`
- [ ] 登录密码是否仍是默认 xuran888？**建议第一次登录后立即改**
- [ ] webhook URL 是否泄漏在日志里？搜日志：`grep -r '<token>' /var/log/fliggy-monitor/`（应无）
- [ ] cookies.json 是否被误传 git？`git log --all -- /etc/fliggy-monitor/cookies.json`（应无）

---

## 7.8 应急联系信息（占位）

| 项 | 值 |
|---|---|
| VPS 提供商 | 待填 |
| 控制台 URL | https://107.172.144.102:port（VPS 提供商） |
| 域名注册商 | 待填 |
| SSH 密钥备份位置 | 本机 `~/.ssh/fliggy-vps.pem`（如果有） |

---

## 7.9 紧急停止 / 重启

```bash
# 全停
ssh root@107.172.144.102 << 'EOF'
systemctl stop fliggy-web
systemctl stop fliggy-monitor.timer
systemctl stop fliggy-cookies-refresh.timer
EOF

# 全起
ssh root@107.172.144.102 << 'EOF'
systemctl start fliggy-web
systemctl start fliggy-monitor.timer
systemctl start fliggy-cookies-refresh.timer
EOF

# 单服务重启
ssh root@107.172.144.102 'systemctl restart fliggy-web'
```

---

## 7.10 接下来的运维建议

- 部署完成后**改默认密码** xuran888
- 部署完成后**配置 webhook**（钉钉/企微/Bark 任一）
- **每周**看一次 `/alerts` 页面，确认告警频率合理
- **每月**看一次 `/settings` → cookie 续期历史，确认 success=1 占比 > 95%
- **每季度**做一次系统补丁 + 安全 checklist
- **每年**做一次 DB 归档（`alerts` 表 1 年前数据 → `alerts_archive.jsonl`）