# 02 · Booktips API — 预订须知 + 商家信息

> 用户在飞猪 H5 上点某个 SKU 的「预订须知」按钮，前端会调这个 API。响应里同时含**预订须知文本**和**商家/旅行社信息**。

## Endpoint

```
GET https://h5api.m.taobao.com/h5/mtop.fliggy.traveldetail.ticket.booktips.new.get/1.0
```

## Query parameters

| key | value | 备注 |
|---|---|---|
| `type` | `originaljson` | 固定 |
| `data` | URL-encoded JSON | 见下 |
| `ttid` | `201300@travel_h5_3.1.0` | 固定 |
| `appKey` | `12574478` | 固定 |
| `t` | `<unix-ms>` | |
| `sign` | `<md5 hex>` | 见 [`03-cookies-and-sign.md`](03-cookies-and-sign.md) |

## data 参数

```json
{
  "itemId":          "1065739764221",
  "skuId":           "6276363111198",
  "poiId":           "1345",
  "needProductPlay": true,
  "pageSource":      "standard_shelf",
  "userFlowParam":   "{\"spmDetail\":\"\",\"clientReqTime\":\"2026-08-23 12:57:33.142\"}",
  "h5Version":       "1.0.26"
}
```

- `itemId + skuId + poiId`：从 shelf.cell 取
- `needProductPlay`：实测 `true` 是默认值；`false` 也行
- `userFlowParam`：内嵌 JSON 字符串，`clientReqTime` 用当前 UTC 时间

## 完整请求示例

```
GET https://h5api.m.taobao.com/h5/mtop.fliggy.traveldetail.ticket.booktips.new.get/1.0?type=originaljson&data=%7B%22itemId%22%3A%221065739764221%22%2C%22skuId%22%3A%226276363111198%22%2C%22poiId%22%3A%221345%22%2C%22needProductPlay%22%3Atrue%2C%22pageSource%22%3A%22standard_shelf%22%2C%22userFlowParam%22%3A%22%7B%5C%22spmDetail%5C%22%3A%5C%22%5C%22%2C%5C%22clientReqTime%5C%22%3A%5C%222026-08-23%2012%3A57%3A33.142%5C%22%7D%22%2C%22h5Version%22%3A%221.0.26%22%7D&ttid=201300%40travel_h5_3.1.0&appKey=12574478&t=1787461053147&sign=65cca6b84109061cfa052541816e1fa2
```

## Headers（与 shelf 同）

```
referer: https://market.m.taobao.com/
origin:  https://market.m.taobao.com
user-agent: <chrome ua>
cookie: <4 必需>
```

---

## Response 结构（重点字段）

```json
{
  "api": "mtop.fliggy.traveldetail.ticket.booktips.new.get",
  "ret": ["SUCCESS::调用成功"],
  "data": {
    "sellerInfo": {
      "data": {
        "title":      "商家说明",
        "sellerName": "敦煌西部新干线旅游专营店",     // ★ 旅行社名
        "sellerIcon": "https://img.alicdn.com/bao/...!!2758902895.png",  // 含 OSS ID，不要拿来当 sellerId
        "icon":       "https://gw.alicdn.com/imgextra/...60-60.png",
        "jumpInfo": {
          "shopJumpUrl": "https://shop144737714.m.taobao.com"  // ★ 店铺 H5 链接
        },
        "sellerPropList": [                                  // ★ 服务人数等属性
          { "propName": "服务人数", "propValue": "38w+" }
        ],
        "sellerTips": [                                     // 顶部 inline 提示文本（3 段拼接）
          { "text": "温馨提示：以下预览信息由【" },
          { "text": "敦煌西部新干线旅游专营店", "textColor": "#6666FF" },
          { "text": "】提供..." }
        ]
      }
    },

    "ticketSkuDesc": {
      "data": {
        "bookTips": [
          {
            "ticketContent": [
              { "contentDescList": [
                  { "contents": [ { "text": "..." } ] }
                ] }
            ]
          }
        ]
      }
    },

    "ticketPreferentialPolicy": {  // 可选，优惠政策（儿童/老人免费规则等）
      "data": {
        "preferentialPolicies": [
          { "type": "儿童", "typeDesc": "【免费】6周岁（含）以下..." }
        ]
      }
    },

    "graphicDetail": { "data": { "graphicHtml": "..." } },  // 详情图片 HTML
    "alimeEntrance": { "data": { "actionUrl": "...", "title": "客服" } },  // 客服入口
    "detailCore":    { "data": { "children": ["ticketSkuDesc", "graphicDetail"] } }
  }
}
```

---

## bookTips 章节顺序（固定 5 段）

```
data.ticketSkuDesc.data.bookTips[].ticketContent[]  →  每段一个 dict
  └─ contentDescList[]
       └─ contents[].text   ← 真实文本
```

5 段固定顺序：
1. 预订说明
2. 费用说明
3. 地址说明
4. 其他说明
5. 温馨提示

⚠️ title 字段偶尔 dict 偶尔 string，处理时 `isinstance` 判一下。

---

## 字段名规则（itemId 级共享）

| 维度 | 观察 |
|---|---|
| 颗粒度 | `sellerInfo` 在 `data` 顶层，**单 itemId 一份**（同一个 itemId 多个 SKU 共用同一个 seller） |
| 与 cell.sellerId 关系 | shelf cell.sellerId 是 canonical；booktips sellerIcon URL 是**另一套 ID**（OSS 路径 6xxx/8xxx），**不要拿来当 sellerId** |
| 跨 POI 套票 | 周边景区 cell 一样走自己 itemId 的 booktips，seller 是 cell 的卖家，不是当前 POI 运营方 |

---

## 关键踩坑

### 1. sellerIcon URL ≠ cell.sellerId

实测例：

```
shelf.cell.sellerId       = "2218193682124"   (cell 字段)
booktips.sellerIcon URL   = "...6000000008087..."   (OSS 路径)
shopJumpUrl host          = "shop241466070"    (淘宝店铺短号)
```

**三套 ID 完全无关**。监控脚本一律用 `cell.sellerId` 作 canonical key，booktips 只补 sellerName/icon/shopUrl。

### 2. sellerIcon 字段有时缺失

部分响应（实测 5/16）只有 `icon` 字段没有 `sellerIcon`。处理：

```python
info = resp["data"]["sellerInfo"]["data"]
seller_icon = info.get("sellerIcon") or info.get("icon")  # 任一即可
```

### 3. shopJumpUrl 是 H5 链接（m.taobao.com），不是 PC 链接

`https://shop144737714.m.taobao.com` 是 H5，浏览器能直接打开；不要用 PC 域名。

---

## 调用代码示例

```python
from mtop_client import MtopClient
import json

cookies = json.load(open("/etc/fliggy-vps/cookies.json"))
client = MtopClient(cookies=cookies)

# cell 来自 shelf 解析结果
raw = client.booktips(
    item_id="1065739764221",
    sku_id="6276363111198",
    poi_id="1345",
)

info = raw["data"]["sellerInfo"]["data"]
print(f"seller: {info['sellerName']}")
print(f"shop:   {info['jumpInfo']['shopJumpUrl']}")
print(f"stats:  {info.get('sellerPropList', [])}")
```

---

## 优化：booktips 命中率

8 POI × 60 cells = 60 cell；按 itemId 去重 = 48 unique itemIds → 16 unique sellers。

**只 hit 16 次 booktips**，不是 48 次。原因：booktips 的 sellerInfo 是 **itemId 级共享**，同一个 itemId 所有 SKU 走同一次 booktips 即可。

```python
# 增量策略：只 cache miss 时 hit
seen_itemIds = set()
for cell in all_cells:
    if cell.itemId in seen_itemIds: continue
    seen_itemIds.add(cell.itemId)
    raw = client.booktips(cell.itemId, cell.skuId, cell.poiId)
    seller_cache.update(extract_seller_info(raw))
```

进一步去重：按 sellerId 分组，**每个 unique seller 只 hit 一次**（48 → 16）。详见 [`../README.md`](../README.md) 「已落地的关键结论 #4」。

---

## 实测响应大小

每个 booktips 响应 ~5 KB。16 个 seller 一轮全 hit = 80 KB。30 min 一轮。VPS 几乎无压力。