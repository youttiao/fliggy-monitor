"""Fliggy H5 mtop API 常量。新项目从这里 import，不要 hard-code。

Source: 浏览器 Network 面板抓包 + curl 复现验证
Updated: 2026-08-23
"""

# ── mtop gateway ────────────────────────────────────────────────────
MTOP_GATEWAY = "https://h5api.m.taobao.com/h5"
APP_KEY = "12574478"
TTID = "201300@travel_h5_3.1.0"

# ── 业务 API ────────────────────────────────────────────────────────
# shelf：POI 详情页货架（门票套餐 + 园内项目 + 周边景区）
SHELF_API = f"{MTOP_GATEWAY}/mtop.trip.serverless.api.gateway/2.0"
SHELF_VERSION = "2.0"

# booktips：预订须知 + 商家信息（itemId 级共享）
BOOKTIPS_API = f"{MTOP_GATEWAY}/mtop.fliggy.traveldetail.ticket.booktips.new.get/1.0"
BOOKTIPS_VERSION = "1.0"

# ── 必要 Cookie（4 个，未登录态也能 200）──────────────────────────
REQUIRED_COOKIES = [
    "_m_h5_tk",       # sign token（含 _<expiry> 后缀）
    "_m_h5_tk_enc",   # 配合验签
    "cookie2",        # 32-char hex
    "t",              # mtop session token（≠ URL 里的 t 时间戳）
]

# ── 必带请求头 ────────────────────────────────────────────────────
REQUIRED_HEADERS = {
    "referer": "https://market.m.taobao.com/",
    "origin":  "https://market.m.taobao.com",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/154.0.0.0 Safari/537.36",
}

# ── Shelf fc 参数（fcGroup=fl-channel-data, fcName=ticketPoi）─────
SHELF_FC_GROUP = "fl-channel-data"
SHELF_FC_NAME = "ticketPoi"
SHELF_DATA_TYPE_SHELF = "shelf"

# ── Booktips data 字段（部分固定值）───────────────────────────────
BOOKTIPS_DEFAULT_DATA = {
    "needProductPlay": True,
    "pageSource": "standard_shelf",
    "h5Version": "1.0.26",
}

# ── Self sellerId baseline ─────────────────────────────────────────
SELF_SELLER_ID = "2217592322543"