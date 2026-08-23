# 03 · Cookies + mtop Sign 算法

> VPS 抓飞猪最核心的两件事：① 4 个必需 Cookie，② mtop sign 复现。

---

## 1. 必需 Cookie（4 个就够）

从浏览器（mac Chrome 实测）抓出以下 cookie：

| Cookie key | 形如 | 作用 |
|---|---|---|
| `_m_h5_tk` | `75e43700f8abc0a74c65078d898c5c18_1787466255406` | ★ **核心** — 含 token（前半段，32 hex）+ expiry（后半段，13 位 unix-ms） |
| `_m_h5_tk_enc` | `0148e457b4d114c9241c5d25ef3360ee` | 32 hex；配合验签 |
| `cookie2` | `198a5686620872d3855b9884d702c2cc` | 32 hex；mtop session |
| `t` | `efa270ed8840101a88a935a9f0f5a6fe` | 32 hex；mtop session token（≠ URL 里 t 时间戳那个） |

⚠️ **`t` cookie 和 URL 里的 `t` query 参数是两个东西**，别混了。

## 2. 不需要的 Cookie（可选）

实测不带这些依然 `200 SUCCESS`：

- `isg` / `lgc` / `dnk` / `tracknick` / `uc3` / `uc4` — 登录态相关，未登录态拿 shelf/booktips 也行
- `_w_tb_nick` / `_w_app_lg` — 客户端类型标记

**登录态的影响**：
- ✅ 拿到会员价 / 专属优惠券 / 历史浏览
- ❌ 不影响 seller 列表 / 价格 / 销量 / 商家信息

监控脚本默认**未登录**即可，省心。

## 3. 怎么从浏览器抓 cookies

### 方案 A：DevTools Network 面板（一次性）

1. 打开 mac Chrome → F12 → Network
2. 访问 `https://outfliggys.m.taobao.com/app/trip/rx-trip-ticket/pages/detail?poiId=1345`
3. 找到任意一个 `mtop.fliggy.traveldetail.ticket.booktips.new.get` 请求
4. 右键 → Copy → Copy as cURL (bash)
5. 从 `-H 'cookie: ...'` 里抽 4 个 key

### 方案 B：Chrome extension（自动化）

新项目建议实现一个 cookie 自动 fetcher：playwright 跑 headless Chrome，访问淘宝 H5，把 cookies 写到 `/etc/fliggy-vps/cookies.json`。每 90 min 续期一次。

---

## 4. mtop Sign 算法

```python
import hashlib

def mtop_sign(token: str, t_ms: str, app_key: str, data_str: str) -> str:
    return hashlib.md5(f"{token}&{t_ms}&{app_key}&{data_str}".encode()).hexdigest()
```

### 关键点

| 参数 | 来源 |
|---|---|
| `token` | `_m_h5_tk` cookie 的**前半段**（去掉 `_expiry`）。例：`75e43700f8abc0a74c65078d898c5c18` |
| `t_ms` | 当前 unix 毫秒时间戳。例：`1787461053147` |
| `app_key` | 固定 `12574478` |
| `data_str` | data 参数的**原始 JSON 字符串**（不要 sort_keys，不要 ensure_ascii 之外的花活） |

### data 字符串的精确格式

```python
import json
data_obj = {
    "fcGroup": "fl-channel-data",
    "fcName":  "ticketPoi",
    "fcData":  {"dataType": "shelf", "poiId": "1345"},
    "source":     "standard_shelf",
    "pageSource": "standard_shelf",
    "h5Version":  "1.0.26",
}
data_str = json.dumps(data_obj, separators=(",", ":"), ensure_ascii=False)
# 输出: {"fcGroup":"fl-channel-data","fcName":"ticketPoi","fcData":{"dataType":"shelf","poiId":"1345"},"source":"standard_shelf","pageSource":"standard_shelf","h5Version":"1.0.26"}
```

然后 URL 编码：

```python
from urllib.parse import quote
quoted = quote(data_str, safe="")
```

**关键约束**：
- 字段顺序可任意（不要 sort_keys），但要和 sign 算时的字符串 byte-for-byte 一致
- 中文字符不被转义（`ensure_ascii=False` + `quote(safe="")`）
- 不要在 JSON 里加多余空格（`separators=(",", ":")`）

### 验证脚本

把以下保存为 `verify_sign.py` 跑一次：

```python
import hashlib
from urllib.parse import unquote

token = "75e43700f8abc0a74c65078d898c5c18"
t_ms = "1787461053147"
app_key = "12574478"

# 从浏览器抓的 URL 里抽 data 部分
data_encoded = "%7B%22fcGroup%22%3A%22fl-channel-data%22%2C%22fcName%22%3A%22ticketPoi%22%2C%22fcData%22%3A%7B%22dataType%22%3A%22shelf%22%2C%22poiId%22%3A%221345%22%7D%2C%22source%22%3A%22standard_shelf%22%2C%22pageSource%22%3A%22standard_shelf%22%2C%22h5Version%22%3A%221.0.26%22%7D"
data_str = unquote(data_encoded)

sign = hashlib.md5(f"{token}&{t_ms}&{app_key}&{data_str}".encode()).hexdigest()
print(f"computed: {sign}")
# 应该 = 浏览器 URL 里的 sign 值（例：7d55caad83bb5b83ffefef82e6b6aa12）
```

---

## 5. sign 时间窗 + 续期策略

| 项 | 实测值 |
|---|---|
| `_m_h5_tk` 有效期 | ~2 小时（滑动窗） |
| sign 复用条件 | `_m_h5_tk` 的 `_expiry` 后缀之前都有效 |
| `t` 参数 | 当前 unix-ms（不能复用旧值） |
| 续期触发 | server 返回 `FAIL_SYS_SESSION_EXPIRED` 或 HTTP 403 |

### 推荐续期机制

```python
# 每 90 min 触发一次 refresh
import threading
def schedule_refresh(client):
    def _refresh():
        while True:
            time.sleep(90 * 60)
            try:
                new_cookies = playwright_fetch_cookies()
                client.cookies.update(new_cookies)
                # 更新 _m_h5_tk 后，client._token 也得更新
                client._token = new_cookies["_m_h5_tk"].split("_", 1)[0]
            except Exception as e:
                print(f"[refresh FAIL] {e}")
    threading.Thread(target=_refresh, daemon=True).start()
```

---

## 6. 完整请求示例（curl 直跑）

```bash
TOKEN="75e43700f8abc0a74c65078d898c5c18"
COOKIE="_m_h5_tk=75e43700f8abc0a74c65078d898c5c18_1787466255406; _m_h5_tk_enc=0148e457b4d114c9241c5d25ef3360ee; cookie2=198a5686620872d3855b9884d702c2cc; t=efa270ed8840101a88a935a9f0f5a6fe"

T_MS=$(date +%s%3N)
DATA='{"fcGroup":"fl-channel-data","fcName":"ticketPoi","fcData":{"dataType":"shelf","poiId":"1345"},"source":"standard_shelf","pageSource":"standard_shelf","h5Version":"1.0.26"}'

# URL 编码 data（用 jq 或者 python）
DATA_ENC=$(python3 -c "from urllib.parse import quote; print(quote('$DATA', safe=''))")

# 计算 sign
SIGN=$(python3 -c "
import hashlib
print(hashlib.md5(f'$TOKEN&$T_MS&12574478&$DATA'.encode()).hexdigest())
")

# 发请求
/usr/bin/curl -sS \
  -H "referer: https://market.m.taobao.com/" \
  -H "origin: https://market.m.taobao.com" \
  -H "user-agent: Mozilla/5.0 ..." \
  -H "cookie: $COOKIE" \
  "https://h5api.m.taobao.com/h5/mtop.trip.serverless.api.gateway/2.0?type=originaljson&data=$DATA_ENC&ttid=201300%40travel_h5_3.1.0&appKey=12574478&t=$T_MS&sign=$SIGN"
```

---

## 7. 必带 Headers

| Header | 值 | 必需 |
|---|---|---|
| `referer` | `https://market.m.taobao.com/` | ✅ |
| `origin` | `https://market.m.taobao.com` | ✅ |
| `user-agent` | 标准 Chrome UA | ✅ |
| `cookie` | 4 个必需 cookie | ✅ |
| `b-fpt` | `ftuid(...)` | ❌ 实测 |
| `warehousecode` | JSON 字符串 | ❌ 实测 |

`b-fpt` 和 `warehousecode` 是浏览器指纹头；实测不带 server 不拒。但带更稳，建议全量带上。

---

## 8. 错误响应处理

| ret / HTTP | 含义 | 处理 |
|---|---|---|
| `SUCCESS::调用成功` | OK | 正常 |
| `FAIL_SYS_SESSION_EXPIRED::Session过期` | _m_h5_tk 过期 | 触发 cookie 续期 |
| `FAIL_SYS_ILLEGAL_ACCESS::非法请求` | sign / referer 错 | 检查 sign 算法 |
| `FAIL_SYS_SERVICE_FLOW_LIMIT::限流` | 请求太频繁 | 退避 30s 后重试 |
| HTTP 403 | 多半是 sign 错或 cookie 失效 | 重算 sign + 续期 |
| HTTP 5xx | server 端问题 | 退避后重试 3 次 |

实现参考 [`../code/mtop_client.py`](../code/mtop_client.py) `MtopClient._request`。