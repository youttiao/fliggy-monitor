# 04 · Shelf Ticket Filter — 门票过滤规则

> shelf API 返回的 shelves 数组里有 10+ 种 type，**只有 `ScenicTicketType` 才有预订须知入口**。本文档讲怎么过滤出「真正要监控的门票 cell」。

---

## 双条件过滤（一行规则搞定所有门票）

```python
def iter_ticket_cells(shelves):
    for shelf in shelves:
        if shelf.get("type") != "ScenicTicketType":
            continue
        # cells 可能在顶层 也可能在 tabs[].cells
        cells = list(shelf.get("cells", []))
        for tab in shelf.get("tabs", []):
            cells.extend(tab.get("cells", []))
        for cell in cells:
            if not cell.get("bookingTipsJumpInfo"):
                continue  # 免费票 / 老人儿童免费票 等
            yield shelf, cell
```

完整实现：[`../code/mtop_client.py`](../code/mtop_client.py) `parse_ticket_cells()`。

---

## 为什么是「双条件」？

### 条件 1：`type == "ScenicTicketType"`

排除其他 9+ 种 type（OneDayTripType / GroupTripType / TravelPhotoType / RouteNarratorType / Expert / CharterCarType / HotelScenicType / hotelShelf / PlayFunType）。

实测：这些 type 的 cell **没有 `bookingTipsJumpInfo`**。

### 条件 2：`cell.bookingTipsJumpInfo is not None`

排除 ScenicTicketType 里的**免费票 / 免预约票 / 仅身份证票**等。

实测：
- 圆明园「儿童票(年龄18周岁(不含)以下) 免费」 — 无 bookingTipsJumpInfo
- 圆明园「老人票(年龄60周岁(含)以上) 免费」 — 无 bookingTipsJumpInfo
- 其他 POI 类似

**为什么要排除**：
1. 免费票没有卖家（不是商品，是优惠政策）
2. 监控「外部商家在卖票」的目的不包含免费票
3. hit booktips 也只会返回优惠政策，不含 sellerInfo

---

## ScenicTicketType 的 shelf.name 多样性

实测 8 POI（数据 2026-08-23）：

| POI | shelves 里的 ScenicTicketType title 列表 |
|---|---|
| 圆明园 (1345) | 景点门票, 门票套餐, 园内项目 |
| 北京动物园 (1552) | 门票套餐, 园内项目, **周边景区门票, 周边景区套票** |
| 天坛 (1350) | 门票套餐, 门票套餐, 园内项目, 景区联票 |
| 颐和园 (1355) | 门票套餐, 园内项目 |
| 景山 (1341) | 门票套餐, 园内项目 |
| 北海 (1338) | 门票套餐, 园内项目 |
| 雍和宫 (1331) | 门票套餐, 园内项目 |
| 恭王府 (1544) | 门票套餐, 园内项目 |
| 藏文化博物院 (12726) | 门票套餐, 园内项目 |

- 「周边景区门票」/「周边景区套票」是**跨 POI 组合产品**，type 仍是 ScenicTicketType
- cell.poiId **不一定是当前 POI 的 poiId** —— **存数据要用 cell.poiId + cell.poiName**，不要用 shelf 级

---

## 跨 POI 去重

同一个 itemId 可能出现在多个 POI 的 shelf 里：

- 北京海洋馆（cell.poiId=140626495）同时出现在 北京动物园 shelf（poiId=1552）的「周边景区门票」
- 部分 itemId 跨 cell 共享同一份 booktips 响应（itemId 级共享 sellerInfo）

**策略**：
- 数据落盘时按 `(poiId, itemId, skuId)` 三元组去重，存 POI 维度全量
- booktips 调用时按 **unique itemId** 去重（60 cells → 48 unique → 16 unique sellers → 16 booktips 调用）

---

## cells 嵌套两种位置

### 情况 A：cells 在顶层

```json
{
  "type": "ScenicTicketType",
  "name": "门票套餐",
  "cells": [
    { "itemId": "...", "sellerId": "...", ... },
    ...
  ]
}
```

### 情况 B：cells 在 tabs[].cells

```json
{
  "type": "ScenicTicketType",
  "name": "景点门票",
  "tabs": [
    {
      "name": "成人票",
      "cells": [
        { "itemId": "...", ... }
      ]
    },
    {
      "name": "儿童票",
      "cells": [
        { "itemId": "...", ... }
      ]
    }
  ]
}
```

**两个路径都要遍历**，否则漏一半数据。详见 [`../code/mtop_client.py`](../code/mtop_client.py) `parse_ticket_cells` 第 60-65 行。

---

## 免费票样例（要跳过的）

```
# 圆明园 shelf 里有
cell: {"name": "儿童票(年龄18周岁(不含)以下)", "priceStruct": {"integerPrice": "0", "pricePrefix": "¥"}, "bookingTipsJumpInfo": null}
cell: {"name": "老人票(年龄60周岁(含)以上)", "priceStruct": {"integerPrice": "0", "pricePrefix": "¥"}, "bookingTipsJumpInfo": null}
```

**识别**：直接 `if not cell.get("bookingTipsJumpInfo"): continue`，不要尝试 hit booktips。

---

## 自营判定

通过 `cell.sellerId == SELF_SELLER_ID` 判定：

```python
SELF_SELLER_ID = "2217592322543"  # 北京旭冉假期旅游专营店

is_self = (cell["sellerId"] == SELF_SELLER_ID)
```

详见 [`../data/seller_baseline.json`](../data/seller_baseline.json)。

---

## 解析后输出格式

```python
@dataclass
class ParsedCell:
    poiId: str           # cell.poiId（**关键：用 cell 不用 shelf**）
    poiName: str         # cell.poiName
    itemId: str
    skuId: str
    name: str            # 票名（如 "大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解"）
    price: str           # 拼接后："¥58" / "¥59.5"
    priceDecimal: str    # 小数部分（".0" / ".5" / None）
    sold: str            # 销量字符串（"1234" / "1.2w+"）
    cellType: str        # shelf.name（"门票套餐" / "周边景区套票" 等）
    sellerId: str        # ★ 商家 ID（13 位 2217xxx）
```

`price` 拼接：

```python
ps = cell.get("priceStruct", {}) or {}
price = f"{ps.get('pricePrefix', '¥')}{ps.get('integerPrice', '')}{ps.get('priceSuffix', '')}"
# 例: "¥58" / "¥59起"
```

⚠️ 不要直接拼字符串做算术；浮点计算用 `decimal` 模块。

---

## 实战：从 raw 拿所有「要监控的 cell」

```python
from mtop_client import MtopClient, parse_ticket_cells

client = MtopClient(cookies={...})
raw = client.shelf("1345")  # 圆明园

cells = parse_ticket_cells(raw)
print(f"圆明园: {len(cells)} cells to monitor")

for c in cells:
    print(f"  poi={c['poiName']:6s} item={c['itemId']:13s} "
          f"seller={c['sellerId']:13s} {c['price']:8s} {c['name']}")
```

输出示例：

```
圆明园: 6 cells to monitor
  poi=圆明园 item=1065739764221 seller=2215602156137 ¥58     大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解
  poi=圆明园 item=775607420214  seller=2216517143254 ¥98     圆明园+西郊机场线地铁票
  ...
```

---

## 总结 checklist

✅ 用 `type == "ScenicTicketType"` 过滤 type
✅ 用 `bookingTipsJumpInfo is not None` 过滤免费票
✅ 同时遍历顶层 `cells` 和 `tabs[].cells`
✅ 存数据用 `cell.poiId/poiName`，不用 shelf 级
✅ 按 `(poiId, itemId, skuId)` 去重
✅ booktips 调用按 unique itemId 去重（→ 16 calls，不是 60）