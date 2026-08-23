# 06 · 30 min 轮询频率策略

> 用户要求"每个景区半个小时查一次"。本节解释为什么是 30 min、怎么实现、人为抖动怎么打、Cookie 续期怎么配合。

---

## 6.1 为什么 30 min

| 维度 | 30 min 的合理性 |
|---|---|
| **业务时效性** | 飞猪 H5 POI 详情页缓存通常 ≥ 5 min（实测），新 SKU 上架、卖家下架通常是分钟-小时级。30 min 足够抓到主要变化 |
| **反爬压力** | 8 POI × 每 30 min = 16 qps 平均，峰值（每 POI 间隔 0.5s 随机抖动）< 2 qps，远低于飞猪限流（实测未登录态 1 qps 无感） |
| **带宽** | 每轮 ~1.3 MB raw + ~80 KB booktips；日带宽 < 70 MB，1 GB 月流量套餐绰绰有余 |
| **告警延迟** | 最坏情况，用户 30 min 后才知道。但通过 webhook 实时推送（不等下一轮），时效 ≤ 30 min |
| **运维负担** | 日志量、DB 增长、CPU 都极低，1 vCPU VPS 跑 100 个 POI 都不卡 |

**如果以后想加快**：把 `polling_sec` 改 300（5 min）。注意 cookie 续期频率也要跟上（建议 60 min）。

---

## 6.2 调度实现（systemd timer）

### 6.2.1 `fliggy-monitor.timer`（触发器）

```ini
[Unit]
Description=Fliggy Monitor runs every 30 min

[Timer]
# 启动 2 min 后第一次跑
OnBootSec=2min

# 每次跑完 30 min 后下一次（绝对间隔，不受 run 耗时影响）
OnUnitActiveSec=30min

# 替代方案：用日历表达式（每个整点 + 半点）
# OnCalendar=*:0/30

# 错过的任务补跑（VPS 重启后）
Persistent=true

# 容差 10s（systemd 实际触发时间允许 ± 10s 浮动）
AccuracySec=10s

[Install]
WantedBy=timers.target
```

> **两种触发方式选一种**：
> - `OnUnitActiveSec=30min`：**相对上一轮结束的 30 min**（推荐）——每轮稳定 30 min 间隔
> - `OnCalendar=*:0/30`：**整点 + 半点**——可能与别的运维任务冲突

### 6.2.2 `fliggy-monitor.service`（执行体）

```ini
[Service]
Type=oneshot           # 跑完就退出，不 long-run
User=monitor
ExecStart=/opt/fliggy-monitor/.venv/bin/python3 /opt/fliggy-monitor/code/fliggy_monitor.py
```

`Type=oneshot` 意味着脚本是「一轮扫完就退出」的设计——更稳（无状态泄漏），systemd 负责调度和失败重启。

### 6.2.3 三套调度时间线

```
T+0        boot
T+2min     ★ 第 1 轮启动 (OnBootSec)
T+~5s      第 1 轮结束
T+32min    ★ 第 2 轮启动 (OnUnitActiveSec=30min)
T+32min+5s 第 2 轮结束
...
T+?        cookie 续期（独立 timer，每 90 min）
```

---

## 6.3 轮内行为（30 min 间隔下，一轮扫描里做什么）

```
T+0.0s   加载 SQLite、pois、sellers、cookies
T+0.1s   解析 cookies，更新 client._token

# ── 8 POI 依次扫（每个 POI 之间 sleep random(0.2, 0.8)）──
T+0.2s   shelf poi=1345 (圆明园)
T+0.7s   parse → 6 cells (含跨 POI cell)
T+0.7s   对 6 cells 中 cache miss 的 sellerId → booktips（并行触发）
T+1.0s   sleep(0.3)
T+1.3s   shelf poi=12726 (藏文化博物院)
...
T+5.0s   shelf poi=1544 (恭王府)

# ── 写库 ──
T+5.5s   INSERT rounds（started_at = now, status=running）
T+5.6s   UPSERT 60 cells_snapshot（每个 cell）
T+6.0s   UPSERT sellers（触发器自动累加 last_seen）
T+6.5s   比较本轮 vs 上轮 → 生成 alerts（INSERT OR IGNORE by dedup_key）
T+7.0s   推送 webhook（异步队列，不阻塞）

T+7.5s   UPDATE rounds SET finished_at=now, status='success', duration_ms=7500

T+7.5s   退出
```

**每轮总耗时**：~7-10s（8 POI），远小于 30 min 间隔。

---

## 6.4 抖动（避免被风控）

### 6.4.1 POI 间抖动

```python
import random, time
for poi in pois:
    monitor_one_poi(poi)
    time.sleep(random.uniform(0.2, 0.8))  # 每个 POI 之间 200-800ms 随机
```

### 6.4.2 启动抖动（避免每次都在整 :00 启动）

systemd 的 `AccuracySec=10s` 已经允许 ±10s 浮动。如果想更随机：

```python
import random, time
# 进入主循环前，等待 0-30s 随机
delay = random.uniform(0, 30)
time.sleep(delay)
```

或用 systemd `RandomizedDelaySec=60`：

```ini
[Timer]
OnUnitActiveSec=30min
RandomizedDelaySec=30
# 实际触发时间在 [T+30min, T+30min+30s) 之间随机
```

### 6.4.3 凌晨降频（避免被识别为机器）

凌晨 0-6 点改为 1 h 一轮，6-9 点 / 21-24 点改为 30 min：

```python
import datetime
def get_interval_sec(now=None):
    now = now or datetime.datetime.now()
    h = now.hour
    if 0 <= h < 6:    return 3600      # 凌晨：1 h
    elif 6 <= h < 9:  return 1800      # 早高峰：30 min
    elif 9 <= h < 21: return 1800      # 日间：30 min
    else:             return 1800      # 晚间：30 min
```

> ⚠️ 但 v1 简化版：全部 30 min。后续看 cookie 续期稳不稳再决定要不要分时段。

---

## 6.5 POI 个别化频率（后续扩展）

`pois` 表已有 `polling_sec` 字段。v2 可让每个 POI 用不同频率：

```sql
-- 比如热门圆明园 30 min，冷门藏文化博物院 1 h
UPDATE pois SET polling_sec = 1800 WHERE poi_id = '1345';
UPDATE pois SET polling_sec = 3600 WHERE poi_id = '12726';
```

调度逻辑：

```python
# 启动时把所有 POI 装进优先队列
# scheduler loop:
while True:
    now = datetime.now()
    for poi in pois:
        if poi.enabled and now - poi.last_scanned_at >= timedelta(seconds=poi.polling_sec):
            monitor_one_poi(poi)
            time.sleep(random.uniform(0.2, 0.8))
```

> v1 用 systemd timer 全量触发，所有 POI 同频；v2 改 in-process scheduler。

---

## 6.6 Cookie 续期配合（90 min vs 2 h）

| 项 | 值 |
|---|---|
| `_m_h5_tk` 有效期 | ~2 h（实测 sliding window） |
| 续期触发器 | `fliggy-cookies-refresh.timer` 每 **90 min** |
| 续期超时 | playwright 启动 ≤ 30s，访问 H5 ≤ 15s，写 cookie ≤ 1s |
| 失败重试 | 内部 3 次 + 间隔指数退避；最终失败 → 写 `cookie_refresh_failed` alert |

**为什么 90 min 不是 2 h**：留 30 min buffer；续期脚本本身可能耗时 / 失败，留余量。

### 续期 vs 监控时间错开

续期是 90 min 一轮（与监控不同步），监控读 cookies.json 时读到的是上一刻的版本。每次续期后**新写入文件**，监控下次读时拿到新值。

**注意点**：监控在跑的那几秒钟里不要续期（避免读到半写的文件）。最简单做法：监控 `Type=oneshot` 跑完就退出，与续期 timer 自然错开 99%。

---

## 6.7 异常情况的频率降级

| 异常 | 处理 |
|---|---|
| 某 POI shelf 连续 3 轮 5xx | 该 POI 暂停扫描 1 h，避免连发请求给风控加重 |
| cookie 续期失败 2 次 | 整个监控暂停（systemd 触发 `webhook_status=critical`），等手工修复 |
| 网络断开 | mtop_client 重试 3 次（2s/8s/30s backoff）；最终失败 = 本 POI skip，本轮 status='partial' |
| VPS 重启 | systemd `Persistent=true` 会补跑错过的任务，但只在「错过的那 30 min」里跑一次（不会连环补） |

---

## 6.8 时区

- 数据库所有 `ts` 字段存 **UTC ISO 8601**（例 `2026-08-23T06:00:00+00:00`）
- 前端展示按 config.site_timezone（默认 `Asia/Shanghai`）转 `+08:00`
- systemd 跑的脚本用 UTC，但 cookie 续期 / 业务日历按上海时间（影响"凌晨降频"判定）
- 日志统一 UTC + prefix 显示本地时区（`journalctl` 默认本地）

```python
from datetime import datetime, timezone, timedelta
SHANGHAI = timezone(timedelta(hours=8))
def now_shanghai(): return datetime.now(SHANGHAI).isoformat()
def now_utc():      return datetime.now(timezone.utc).isoformat()
```

---

## 6.9 频率调试 / 验证

```bash
# 1. 看 timer 调度
ssh root@107.172.144.102 \
    'systemctl list-timers fliggy-monitor.timer fliggy-cookies-refresh.timer --no-pager'

# 2. 看本轮花了多久
ssh root@107.172.104.102 \
    "sqlite3 /opt/fliggy-monitor/data/monitor.db \
        'SELECT round_id, duration_ms, cells_total FROM rounds ORDER BY id DESC LIMIT 10;'"

# 3. 看每轮的非自营数趋势
ssh root@107.172.144.102 \
    "sqlite3 /opt/fliggy-monitor/data/monitor.db \
        \"SELECT strftime('%Y-%m-%d %H:%M', started_at) AS r, cells_non_self \
          FROM rounds WHERE started_at > datetime('now', '-1 day') \
          ORDER BY started_at;\""

# 4. 看 cookie 续期历史
ssh root@107.172.144.102 \
    "sqlite3 /opt/fliggy-monitor/data/monitor.db \
        'SELECT ts, success, substr(token_prefix,1,8) AS tok, error_msg \
         FROM cookies_history ORDER BY ts DESC LIMIT 10;'"
```

---

## 6.10 接下来

- 在 `code/fliggy_monitor.py` 加 `compute_next_interval()` 返回 1800
- `pois` 表已有 `polling_sec` 字段，但 v1 不用——所有 POI 同步
- systemd timer 配 `AccuracySec=10s`
- 续期 timer 独立 90 min
- 不做凌晨降频（v1 简化）