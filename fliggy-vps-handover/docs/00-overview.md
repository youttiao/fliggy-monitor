# 00 · 整体架构 + 数据流

## 监控对象

8 个北京景区，飞猪 H5 POI 详情页能查到的所有门票 SKU：

| POI | poiId | tb.cn |
|---|---|---|
| 圆明园 | 1345 | h.8j2xUJ7 |
| 藏文化博物院 | 12726 | h.89rvDZJ |
| 天坛公园 | 1350 | h.8j2yERj |
| 北海公园 | 1338 | h.89rwfVr |
| 景山公园 | 1341 | h.88dC3tP |
| 颐和园 | 1355 | h.8j2ALQB |
| 雍和宫 | 1331 | h.88dzGP5 |
| 恭王府 | 1544 | h.88WZ60C |

详见 [`../data/poi_registry.json`](../data/poi_registry.json)。

---

## 两个 mtop 接口

```
┌─────────────────┐
│ POI 详情页 (H5) │
└────────┬────────┘
         │
         ├─→ shelf    (mtop.trip.serverless.api.gateway/2.0)
         │    fcGroup=fl-channel-data
         │    fcName=ticketPoi
         │    fcData.dataType=shelf
         │    fcData.poiId=1345
         │
         │   返回: shelves[].cells[]  ← 门票 cell，含 sellerId + 价格 + 库存
         │
         └─→ booktips (mtop.fliggy.traveldetail.ticket.booktips.new.get/1.0)
              data={itemId, skuId, poiId}

              返回: data.sellerInfo.data.sellerName  ← 旅行社 / 专营店 名
                    data.sellerInfo.data.jumpInfo.shopJumpUrl
                    data.sellerInfo.data.sellerPropList  ← 服务人数 Xw+
                    data.ticketSkuDesc.data.bookTips[]   ← 预订须知 5 段
```

---

## 数据流（监控脚本一帧）

```
                        加载静态数据
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
  poi_registry.json    seller_cache.json      seller_baseline.json
  (8 POI)              (16 seller)            (SELF_SELLER_ID)
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              ▼
                   对每个 POI 调 shelf
                              │
                              ▼
                    parse_ticket_cells
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
            自营 cell              非自营 cell
            (跳过)                  (告警)
                                        │
                                        ▼
                              查 seller_cache 补 sellerName
                                        │
                                        ▼
                              cache miss？→ 触发 booktips
                                        │
                                        ▼
                              写 seller_cache.json + 业务落盘
```

---

## 关键模块

| 模块 | 文件 | 职责 |
|---|---|---|
| API 常量 | [`../code/selectors.py`](../code/selectors.py) | URL / appKey / ttid / cookies / 必带头 |
| mtop client | [`../code/mtop_client.py`](../code/mtop_client.py) | sign 计算 + curl 调用 + shelf/booktips parse |
| 监控主循环 | [`../code/fliggy_monitor.py`](../code/fliggy_monitor.py) | 一轮扫 8 POI + 增量 booktips + 告警 |
| 静态数据 | [`../data/`](../data/) | POI 池 + seller 缓存 + baseline |

---

## 输出层（业务相关，每家自己写）

监控脚本模板里只 dump 到 stdout。新项目改成自己的：

- 写 DB（PostgreSQL / MySQL）：每 cell 一行，主键 `(poiId, itemId, skuId)`
- 写时序库（InfluxDB / TDengine）：价格/库存打点
- 推 webhook（钉钉/企微/飞书）：非自营新出现 / 价格异动 / 库存为 0
- 落 OSS / S3：原始 JSON 留底（用于回溯 / 风控分析）

---

## 限制与边界（必须知道）

| 限制 | 说明 |
|---|---|
| **CORS / 跨域** | mtop 域名 `h5api.m.taobao.com` 允许 `https://market.m.taobao.com` referer，curl 带 referer 就行 |
| **rate limit** | 未登录态实测 1 qps 不限流；建议加随机抖动 200-800ms 避免指纹异常 |
| **sign 滑动窗** | `_m_h5_tk` 约 2 小时；过期就 403。需要 cookie 续期机制 |
| **TLS Pinning** | H5 是浏览器栈，**无 Pinning**；App (Flutter + BoringSSL) 有 Pinning，本项目不走 App |
| **登录态可选** | shelf/booktips 未登录 200 SUCCESS；登录态只是让部分字段（如会员价、专属优惠券）出现 |
| **cell 价格精度** | `priceStruct.integerPrice` + `priceStruct.decimalPrice`（例 `59` + `.5` = ¥59.5），不要直接拼字符串 |

---

## 后续工作

详见 [`README.md`](../README.md) 「接下来的工作」章节 + [`06-monitoring-design.md`](06-monitoring-design.md)。