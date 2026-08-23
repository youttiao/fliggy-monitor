# 06 · 监控脚本设计 + 告警规则

> 业务核心：找出「**不是用户自营**」的 SKU 列表，触发告警。

---

## 1. 业务目标（明确）

**用户的监控目的**：哪些 POI 门票 SKU 不是自营在卖（即外部商家/旅行社在卖）。

| 状态 | 含义 | 监控处理 |
|---|---|---|
| `cell.sellerId == "2217592322543"` | 自营（用户自己） | 跳过（不告警） |
| `cell.sellerId != "2217592322543"` | 外部商家 | **告警 / 落盘 / 通知** |

详见 [`../data/seller_baseline.json`](../data/seller_baseline.json)。

---

## 2. 一轮扫描的执行流

```
┌─────────────────────────────────────────────────────────┐
│  init:                                                  │
│    - 加载 poi_registry.json (8 POI)                     │
│    - 加载 seller_cache.json (16 sellers)                │
│    - 加载 seller_baseline.json (SELF_SELLER_ID)         │
│    - 加载 /etc/fliggy-vps/cookies.json                  │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  for each POI in poi_registry:                          │
│    raw = client.shelf(poi.poiId)                        │
│    cells = parse_ticket_cells(raw)                      │
│    for cell in cells:                                   │
│        if cell.sellerId == SELF_SELLER_ID: continue     │
│        # ---- 非自营 cell ----                          │
│        seller = seller_cache.get(cell.sellerId)         │
│        if not seller or not seller.sellerName:          │
│            # cache miss → 触发 booktips                 │
│            raw = client.booktips(cell.itemId, ...)      │
│            seller = parse_seller_info(raw)              │
│            seller_cache[cell.sellerId] = seller         │
│        # 落盘                                           │
│        record = {                                       │
│            timestamp, poiId, poiName,                   │
│            itemId, skuId, name, price, sold,            │
│            sellerId, sellerName, shopJumpUrl, isSelf    │
│        }                                                │
│        emit(record)                                     │
│                                                          │
│    # 限流 + 随机抖动                                     │
│    sleep(random(0.2, 0.8))                              │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  round summary:                                         │
│    - 本轮非自营 cell 总数                                │
│    - 命中 cache / 触发 booktips 数                       │
│    - 失败 cell 数                                       │
│  sleep(round_interval)                                  │
│  repeat                                                 │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 落盘数据结构

每条记录（每 cell 一行）：

```json
{
  "ts":                "2026-08-23T12:30:00.000+08:00",
  "round_id":          "r202608231230",
  "poiId":             "1345",
  "poiName":           "圆明园",
  "itemId":            "1065739764221",
  "skuId":             "6276363111198",
  "name":              "大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解",
  "price":             "¥58",
  "priceDecimal":      ".0",
  "sold":              "1234",
  "cellType":          "门票套餐",
  "sellerId":          "2215602156137",
  "sellerName":        "宫足迹旅行社旗舰店",
  "sellerIcon":        "https://...",
  "shopJumpUrl":       "https://shopNNNN.m.taobao.com",
  "serviceStat":       "服务人数 Xw+",
  "isSelf":            false
}
```

### 落盘格式选择

| 方案 | 优点 | 缺点 |
|---|---|---|
| 每 cell 一行 JSONL（追加） | 简单，回溯容易 | 文件膨胀（30 天 ~30 MB） |
| SQLite 单表 | 查询方便，去重简单 | 需要 schema 设计 |
| Postgres / MySQL | 多机共享，事务支持 | 需要额外服务 |
| OSS / S3 parquet | 长期归档便宜 | 实时查询差 |

**推荐**：短期（30 天）SQLite + 长期归档 OSS parquet。

---

## 4. 告警规则

### 4.1 默认规则（业务核心）

| 规则 | 触发 | 级别 | 通道 |
|---|---|---|---|
| **新 sellerId 出现** | 缓存外的 sellerId 第一次在 shelf 里出现 | 中 | 钉钉 |
| **新 cell 上架** | `(poiId, itemId, skuId)` 第一次出现 | 中 | 钉钉 |
| **自营 cell 突然消失** | 历史上有自营 cell 的 POI 本轮没有自营 | 高 | 钉钉 + 邮件 |
| **价格异动 ±20%** | 同 `(itemId, skuId)` 价格相对上轮变化超 20% | 中 | 钉钉 |
| **库存 0 → >0 / >0 → 0** | 同 cell sold 字段翻转 | 低 | DB 落盘即可 |
| **shelf 连续失败 3 轮** | 任一 POI shelf 连续 3 轮 5xx / 超时 | 高 | 钉钉 + 邮件 + PagerDuty |
| **cookie 续期失败** | refresh 任务失败 | 高 | 钉钉 + 邮件 |
| **监控进程死亡** | systemd 重启触发 | 高 | systemd journal |

### 4.2 告警去重

- 同一 cell 同一规则的告警 **1 小时内不重复发**（去重窗口）
- 价格异动只在「稳定后再次异动」时发（避免抖动）
- 新 sellerId 只发一次（首次出现）

### 4.3 钉钉 webhook 示例

```python
import requests

DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=..."

def send_alert(text: str, level: str = "info"):
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"[Fliggy Monitor] {level}",
            "text":  f"### {level}\n\n{text}"
        },
        "at": {"isAtAll": False},
    }
    requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
```

---

## 5. 轮询频率建议

| POI 类型 | 频率 | 理由 |
|---|---|---|
| 热门 POI（圆明园/颐和园/天坛） | 30 min | 商家活跃 |
| 中等 POI（景山/北海/雍和宫） | 1 h | SKU 变化慢 |
| 冷门 POI（藏文化博物院/恭王府） | 2 h | 数据少 |

按 POI 维度配 `polling_interval` 字段，详见 [`../data/poi_registry.json`](../data/poi_registry.json)（下一版扩展）。

**总带宽估算**（30 min 一轮 × 8 POI）：
- shelf: 8 × 200 KB = 1.6 MB / 30 min ≈ 0.9 KB/s
- booktips: 16 × 5 KB × (cache miss 1 次) ≈ 80 KB / 2 小时 ≈ 0.01 KB/s

VPS 完全无压力。

---

## 6. 频率限制与反爬

| 项 | 建议 |
|---|---|
| 间隔抖动 | 200-800ms 随机（每个 POI 之间） |
| UA | 固定 Chrome 154，不要每轮换 |
| cookie | 90 min 续期；不要每轮都续（异常行为） |
| 监控时段 | 24/7，但避免凌晨 0-6 点突然高频（容易被识别为机器） |
| 单 POI 频率 | 不超过 1 qps |
| 总频率 | 不超过 5 qps |

⚠**关键** —— 飞猪风控监控「行为模式」而非单次请求。固定间隔 + 24/7 完美运行是反模式。要模拟人类作息（详见老项目 CLAUDE.md「操作层规避」）。

---

## 7. 失败的容错

每层都加 retry + circuit breaker：

```python
def shelf_with_retry(client, poi_id, max_retry=3):
    for attempt in range(max_retry):
        try:
            return client.shelf(poi_id)
        except MtopError as e:
            if "SESSION_EXPIRED" in str(e):
                # 触发 cookie 续期（异步）
                trigger_cookie_refresh()
                raise
            if attempt < max_retry - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            raise
```

| 错误 | 处理 |
|---|---|
| `MtopError SESSION_EXPIRED` | 触发 cookie refresh，本轮跳过该 POI |
| `MtopError 限流` | sleep 30s 后重试 3 次 |
| `MtopError 其他 FAIL` | 落盘到 `errors/{poiId}/{timestamp}.json`，告警 |
| `ConnectionError` | sleep 60s 重试 5 次 |
| 其他 Exception | 落盘，告警，本轮跳过 |

---

## 8. 监控自身健康（meta-monitoring）

被监控进程也要被监控：

```bash
# /etc/systemd/system/fliggy-monitor-watchdog.service
[Service]
ExecStart=/bin/bash -c 'while ! pgrep -f fliggy_monitor.py; do echo "DEAD" | mail -s "Fliggy dead" you@example.com; sleep 60; done'
```

或者用 Prometheus node_exporter + AlertManager。

---

## 9. 测试 + 上线 checklist

### 上线前

- [ ] 单元测试 `parse_ticket_cells` / `parse_seller_info` 跑通
- [ ] `tests/smoke.py` 跑通
- [ ] cookie 续期脚本 dry-run 验证
- [ ] 告警 webhook dry-run 验证（自己 @ 一下）
- [ ] systemd service 启停 5 次没异常

### 上线后第一周

- [ ] 看 journalctl 没异常 stack trace
- [ ] 钉钉群里能正常收到告警
- [ ] seller_cache.json 16 个全有 sellerName
- [ ] 数据落盘目录大小符合预期
- [ ] cookies 续期 timer 跑了至少 10 次

### 上线后第一个月

- [ ] 价格异动告警触发频率合理（不刷屏）
- [ ] booktips cache miss 频率降到 < 5%（说明 seller 池稳定）
- [ ] VPS 内存 / CPU 没异常峰值

---

## 10. 进阶（不在 v1 范围）

- **历史价格趋势**：落 DB 后做时序图，监控价格战
- **新商家盯防**：新 sellerId 出现 → 自动拉商家详情（评论/开店时间/历史销量）
- **跨 POI 套票识别**：北京海洋馆出现在 北京动物园 shelf 的「周边景区套票」里 — 这是商家的「打包销售」策略，可以专门统计
- **SKU 维度分析**：同一 itemId 的多个 skuId（如「成人票」「儿童票」），分析价格梯度
- **节假日预测**：春节/十一前 7 天切换到 5 min 一轮

详见 [`../README.md`](../README.md) 「接下来的工作」章节。