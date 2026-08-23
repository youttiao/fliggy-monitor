# 01 · Shelf API — POI 详情页货架

> 飞猪 H5 POI 详情页（`outfliggys.m.taobao.com/app/trip/rx-trip-ticket/pages/detail?poiId=...`）打开后，前端会调这个 mtop 拿货架（门票套餐 / 园内项目 / 周边景区套票）。

## Endpoint

```
GET https://h5api.m.taobao.com/h5/mtop.trip.serverless.api.gateway/2.0
```

## Query parameters

| key | value | 备注 |
|---|---|---|
| `type` | `originaljson` | 固定 |
| `data` | URL-encoded JSON | 核心参数，见下 |
| `ttid` | `201300@travel_h5_3.1.0` | 固定 |
| `appKey` | `12574478` | 固定 |
| `t` | `<unix-ms>` | 当前毫秒时间戳 |
| `sign` | `<md5 hex>` | 见 [`03-cookies-and-sign.md`](03-cookies-and-sign.md) |

## data 参数（核心）

```json
{
  "fcGroup": "fl-channel-data",
  "fcName":  "ticketPoi",
  "fcData": {
    "dataType": "shelf",
    "poiId":    "1345"
  },
  "source":      "standard_shelf",
  "pageSource":  "standard_shelf",
  "h5Version":   "1.0.26"
}
```

- `poiId` 来自 [`../data/poi_registry.json`](../data/poi_registry.json)
- `h5Version` 实测 1.0.26 是当前 H5 的版本号，**不要随便改**，server 会校验

## 完整请求示例（圆明园）

```
GET https://h5api.m.taobao.com/h5/mtop.trip.serverless.api.gateway/2.0?type=originaljson&data=%7B%22fcGroup%22%3A%22fl-channel-data%22%2C%22fcName%22%3A%22ticketPoi%22%2C%22fcData%22%3A%7B%22dataType%22%3A%22shelf%22%2C%22poiId%22%3A%221345%22%7D%2C%22source%22%3A%22standard_shelf%22%2C%22pageSource%22%3A%22standard_shelf%22%2C%22h5Version%22%3A%221.0.26%22%7D&ttid=201300%40travel_h5_3.1.0&appKey=12574478&t=1787461054336&sign=7d55caad83bb5b83ffefef82e6b6aa12
```

## 必带 headers

```
referer: https://market.m.taobao.com/
origin:  https://market.m.taobao.com
user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36
cookie: <4 必需 cookie + 其他任意>
```

不带 `b-fpt` / `warehousecode` 这类浏览器指纹头也能 200 SUCCESS（实测）。

---

## Response 结构

```json
{
  "api": "mtop.trip.serverless.api.gateway",
  "ret": ["SUCCESS::调用成功"],
  "data": {
    "result": {
      "data": {
        "shelf": {
          "shelves": [
            {
              "type": "ScenicTicketType",
              "name": "门票套餐",
              "cells": [
                {
                  "itemId": "1065739764221",
                  "skuId":  "6276363111198",
                  "poiId":  "1345",
                  "poiName": "圆明园",
                  "name":   "大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解",
                  "priceStruct": {
                    "integerPrice": "58",
                    "decimalPrice": ".0",
                    "pricePrefix":  "¥",
                    "priceSuffix":  ""
                  },
                  "soldStr": "1234",
                  "sellerId": "2215602156137",
                  "bookingTipsJumpInfo": {
                    "url": "https://market.m.taobao.com/app/trip/rx-poi-client-pages/pages/notice?itemId=1065739764221&skuId=6276363111198&..."
                  }
                }
              ]
            },
            {
              "type": "ScenicTicketType",
              "name": "景点门票",
              "tabs": [
                {
                  "cells": [...]
                }
              ]
            }
          ]
        }
      }
    }
  }
}
```

> **注意**：`data.result.data.shelf.shelves` 才是真正的数据。其他顶层 key（`api/ret/v`）是 mtop 协议壳。

---

## shelves 类型分布（实测 8 POI）

| type | 含义 | 是否含门票 cell |
|---|---|---|
| `ScenicTicketType` | 门票套餐 / 园内项目 / 周边景区套票 | ✅ 含 `bookingTipsJumpInfo` |
| `OneDayTripType` | 一日游 | ❌ |
| `GroupTripType` | 跟团游 | ❌ |
| `TravelPhotoType` | 旅拍 | ❌ |
| `RouteNarratorType` | 讲解器 | ❌ |
| `Expert` | 当地达人 | ❌ |
| `CharterCarType` | 包车 | ❌ |
| `HotelScenicType` | 酒店 + 景区 | ❌ |
| `hotelShelf` | 酒店 | ❌ |
| `PlayFunType` | 玩乐 | ❌ |

**只有 ScenicTicketType 才有预订须知入口**（`cell.bookingTipsJumpInfo`）。详见 [`04-shelf-ticket-filter.md`](04-shelf-ticket-filter.md)。

---

## cells 嵌套两种位置

```python
# 情况 A：cells 在顶层
shelf = {"type": "ScenicTicketType", "name": "门票套餐", "cells": [...]}

# 情况 B：cells 在 tabs[].cells（例：圆明园景点门票 / 颐和园园内项目）
shelf = {
  "type": "ScenicTicketType",
  "name": "景点门票",
  "tabs": [{"name": "成人票", "cells": [...]}]
}
```

**两个位置都要遍历**，否则漏一半。

---

## cell 字段（核心）

```python
@dataclass
class Cell:
    itemId: str                    # 商品 ID（淘宝电商系统唯一）
    skuId:  str                    # SKU ID
    poiId:  str                    # cell 所属 POI（可能跨 POI — 周边景区套票）
    poiName: str
    name:   str                    # 票名
    priceStruct: {
        integerPrice: str          # 整数部分（"58"）
        decimalPrice: str          # 小数部分（".0" / ".5"）
        pricePrefix:  str          # "¥" / "￥"
        priceSuffix:  str          # "起" / ""
    }
    soldStr: str                   # 销量字符串（"1234" / "1.2w+"）
    sellerId: str                  # ★ 商家账号 ID（13 位 2217xxx）
    bookingTipsJumpInfo: dict      # ★ 有这个 key 才是「真正需要监控的门票」
```

---

## ScenicTicketType 的 shelf.name 多样性（8 POI 实测）

| POI | shelves 里的 ScenicTicketType title 列表 |
|---|---|
| 圆明园 (1345) | 景点门票, 门票套餐, 园内项目 |
| 北京动物园 (1552) | 门票套餐, 园内项目, **周边景区门票, 周边景区套票** |
| 天坛 (1350) | 门票套餐, 门票套餐, 园内项目, 景区联票 |

- 「周边景区门票」/「周边景区套票」是**跨 POI 组合产品**，type 仍是 ScenicTicketType
- cell.poiId **不一定是**当前 POI 的 poiId（**必须用 cell.poiId + cell.poiName 存数据**，不要用 shelf 级）

---

## 实测响应大小

| POI | raw size | cell 数（含周边景区） |
|---|---|---|
| 圆明园 | 184 KB | 6 |
| 北京动物园 | 53 KB | 8（含北京海洋馆） |
| 藏文化博物院 | 62 KB | 5 |
| 雍和宫 | 153 KB | 6 |
| 颐和园 | 212 KB | 10 |
| 景山 | 130 KB | 6 |
| 北海 | 92 KB | 5 |
| 恭王府 | 259 KB | 8 |
| 天坛 | 268 KB | 6 |

8 POI 全量一轮 = ~1.3 MB / 60 cells，30 min 一轮带宽 < 1 KB/s。VPS 几乎无压力。

---

## 错误码

| ret | 含义 | 处理 |
|---|---|---|
| `SUCCESS::调用成功` | OK | 正常处理 |
| `FAIL_SYS_SESSION_EXPIRED::Session过期` | _m_h5_tk 过期 | 触发 cookie 续期 |
| `FAIL_SYS_ILLEGAL_ACCESS::非法请求` | sign 错或 referer 错 | 检查 cookie + headers |
| `FAIL_SYS_SERVICE_FLOW_LIMIT::限流` | 短时间内请求太多 | 加 sleep + 退避 |

---

## 调用代码示例

```python
from mtop_client import MtopClient
import json

cookies = json.load(open("/etc/fliggy-vps/cookies.json"))
client = MtopClient(cookies=cookies)

raw = client.shelf("1345")  # 圆明园
print(len(raw["data"]["result"]["data"]["shelf"]["shelves"]), "shelves")
```