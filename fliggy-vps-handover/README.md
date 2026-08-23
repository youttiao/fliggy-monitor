# Fliggy VPS 监控项目 — Handoff Package

> 来源：携程抓取项目（`/Users/argo/666-XCJ/ctrip-apk-scrap/`）fliggy 抓取实战
> 目标：在 VPS 上跑飞猪 H5 POI 监控（圆明园/藏文化博物院/天坛/北海/景山/颐和园/雍和宫/恭王府），找非自营 SKU
> 数据采集日期：2026-08-23
> 包大小：~30 KB docs + 16 seller 全量 cache + 8 POI 列表 + Python 代码模板

---

## 5 分钟上手

```bash
# 1. 拿包
cp -r /Users/argo/666-XCJ/ctrip-apk-scrap/handover/fliggy-vps/  /path/to/new-project/
cd /path/to/new-project/

# 2. 装依赖（零三方依赖，纯 stdlib + curl）
# Python 3.10+ / curl 7.x 即可

# 3. 注入 cookies（参考 docs/03-cookies-and-sign.md 从浏览器抓）
vim /etc/fliggy-vps/cookies.json   # 4 个 key：_m_h5_tk / _m_h5_tk_enc / cookie2 / t

# 4. 冒烟
python tests/smoke.py
# 预期：6 个非自营 cell，sellerName 拿到

# 5. 起监控
python code/fliggy_monitor.py
```

---

## 包结构

```
fliggy-vps/
├── README.md                      ← 你正在看
├── docs/
│   ├── 00-overview.md             架构 + 数据流
│   ├── 01-api-shelf.md            shelf API 完整规格
│   ├── 02-api-booktips.md         booktips API 完整规格
│   ├── 03-cookies-and-sign.md     cookie 集合 + mtop sign 算法
│   ├── 04-shelf-ticket-filter.md  门票过滤规则（双条件）
│   ├── 05-deployment.md           VPS 部署步骤
│   └── 06-monitoring-design.md    监控脚本设计 + 告警规则
├── data/
│   ├── poi_registry.json          8 POI（poiId + tb.cn 短链 + H5 URL）
│   ├── seller_baseline.json       SELF_SELLER_ID + 自营判定基线
│   └── seller_cache.json          16 个 seller 全量（name/icon/shopUrl/serviceStats）
└── code/
    ├── selectors.py               API 常量（URL / appKey / ttid / headers）
    ├── mtop_client.py             mtop HTTP client + parse helpers
    └── fliggy_monitor.py          主监控循环模板
└── tests/
    ├── smoke.py                   1 个 POI 端到端冒烟
    └── demo_cookies.json          cookies 占位（不要提交真值）
```

---

## 三句话讲明白

**监控什么**：8 个北京 POI 的飞猪 H5 门票列表，每个 SKU（itemId + skuId）谁在卖、价格多少、卖了多少。

**接口**：两个 mtop 接口 — shelf（POI 货架）+ booktips（预订须知 + 商家信息）。**纯 HTTP GET，无 TLS Pinning，未登录态可访问**。VPS curl 直跑，**不需要 Frida / mitm / 抓包**。

**业务核心**：用户自营 sellerId 是 `2217592322543`（北京旭冉假期旅游专营店）。监控目的 = 找「**不是自营在卖**」的 SKU 列表，触发告警。详见 [`data/seller_baseline.json`](data/seller_baseline.json)。

---

## 已落地的关键结论（来自 8/23 抓包实战）

| # | 结论 | 来源 |
|---|---|---|
| 1 | shelf + booktips 都可 curl，未登录 200 SUCCESS | 4-cookie 验证通过 |
| 2 | sign 算法可复现：`MD5(token & t & appKey & data).hexdigest()` | 与浏览器一致 |
| 3 | cell.sellerId 是 canonical ID；booktips sellerIcon URL 是另一套 ID 系统（OSS path），**不要拿来当 sellerId** | icon URL 含 6xxx/8xxx vs cell 13 位 2217xxx |
| 4 | sellerName 是 itemId 级共享，48 cells → 16 unique sellers → 16 booktips 调用 | itemId 跨 cell 共享 |
| 5 | 门票过滤双条件：`type=ScenicTicketType + cell.bookingTipsJumpInfo` | 覆盖「周边景区套票」跨 POI |
| 6 | 16 sellers 中 SELF 仅在 6 个 POI 出现（北海/景山/藏文化博物院/雍和宫/颐和园/北京动物园） | 圆明园/天坛/恭王府 全 3rd-party |

---

## 不在这个包里

- 真实 cookies（敏感，运行时注入）
- 抓包过程截图 / 全量 raw 响应（太大；每 POI shelf ~150KB，booktips ~5KB）
- 老项目的 Recorder / 业务脚本 / 携程相关代码（与飞猪监控无关）

---

## 接下来的工作（按优先级）

1. **P0**：实现 cookie 自动续期（_m_h5_tk ~2 小时滑动窗）。方案：起一个常驻 fetcher，每 90 min 用 playwright 跑一次淘宝 H5 拿新 cookies，写到 `/etc/fliggy-vps/cookies.json`，监控脚本 reload。
2. **P0**：实现告警通道（钉钉/企微 webhook）。
3. **P1**：把 30 min 一轮改成可配置（不同 POI 频率不同：旺季热门 POI 5 min，淡季冷门 1 h）。
4. **P1**：增量 booktips 触发逻辑（只在 cache miss 时 hit）。
5. **P2**：历史价格趋势（落 DB / 时序库）。
6. **P2**：新 seller 出现 → 自动通知（用户在哪个 POI 看到新对手了）。