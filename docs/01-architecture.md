# 01 · 系统架构总览

> 在 `fliggy-vps-handover/` 的抓取/解析代码基础上，加上 Web 控制台、数据库、Webhook 告警通道，部署到 VPS `107.172.144.102`，通过二级域名 `feizhu.19880913.xyz` 访问。

---

## 1.1 业务目标（一句话）

让运营方在浏览器里**实时看到 8 个北京景区在飞猪 H5 上有哪些非自营 SKU 在卖**，并在「出现新对手」「自营缺位」「价格异动」时通过 webhook 把消息推到自己的 IM。

---

## 1.2 三大子系统

```
┌──────────────────────────────────────────────────────────────┐
│                       feizhu.19880913.xyz                       │
│                                                                 │
│  ┌──────────────────────┐        ┌──────────────────────────┐ │
│  │  Web 控制台 (FastAPI) │◄──────►│  监控采集器 (Python 脚本)  │ │
│  │  - 单密码登录          │  共享  │  - 每 30 min 扫 8 POI      │ │
│  │  - 监控配置            │  SQLite │  - shelf + booktips      │ │
│  │  - SKU 实时浏览        │  数据库 │  - 增量 booktips 缓存     │ │
│  │  - 告警历史            │        │  - webhook 推送           │ │
│  │  - 系统状态            │        │                          │ │
│  └─────────┬────────────┘        └──────────┬───────────────┘ │
│            │                                │                  │
│            ▼                                ▼                  │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │                SQLite: /opt/fliggy-monitor/data/         │ │
│  │   rounds / cells_snapshot / sellers / pois / config /    │ │
│  │   alerts / cookies                                       │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                                │
│  ┌──────────────────────┐                                     │
│  │  Cookie 续期器        │  每 90 min 跑一次 playwright       │
│  │  refresh_cookies.py   │  写回 /etc/fliggy-vps/cookies.json │
│  └──────────────────────┘                                     │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼ (webhook POST)
                  ┌─────────────────────────────┐
                  │  用户的 IM 系统              │
                  │  (钉钉/企微/飞书/Bark/Telegram) │
                  └─────────────────────────────┘
```

---

## 1.3 技术栈选型

| 层 | 选型 | 理由 |
|---|---|---|
| **OS** | Ubuntu 22.04 LTS | 中文支持好，systemd 稳定，国内镜像快 |
| **Web 框架** | FastAPI 0.115+ | 异步 / 类型注解 / 自动 OpenAPI 文档 / 单文件可起 |
| **模板** | Jinja2（服务端渲染） | 无构建步骤、单用户、高密度信息 |
| **前端交互** | HTMX 2.0 + Alpine.js 3.x | 局部刷新不写 SPA、Alpine 处理小交互（开关/弹层） |
| **CSS** | 手写 CSS（design tokens） | 见 `frontend-design.md`，无 Tailwind 避免类名臃肿 |
| **数据库** | SQLite 3.45+（WAL 模式） | 单机、单用户、零运维；落盘到 `/opt/fliggy-monitor/data/monitor.db` |
| **HTTP 客户端** | httpx（async）+ curl fallback | 监控脚本沿用 curl（handoff 已有），web 部分用 httpx |
| **Web 服务器** | Uvicorn（worker=2） | FastAPI 原生 ASGI server |
| **反向代理 + TLS** | Caddy 2.x | 自动 HTTPS（Let's Encrypt）+ 反代 + HTTP/2 |
| **进程管理** | systemd | 4 个 unit：web / monitor / cookie-refresh / cookie-refresh.timer |
| **日志** | journald + logrotate | systemd 标准生态 |
| **Playwright** | Chromium headless | cookie 自动续期 |
| **Webhook 客户端** | httpx（带 retry/backoff） | 失败重试 3 次 |

**为什么不用 React/Vue/SPA**：单用户工具，无 SEO 需求，无构建步骤 = 改完即生效；前端 < 100KB JS（HTMX 14KB + Alpine 15KB）；运营工具的"高信息密度 + 表格"是服务端渲染的强项。

**为什么 SQLite 不用 PostgreSQL**：单 VPS、单写者、读 < 10 qps，SQLite 完全够；零运维（不用 initdb、不用 pg_ctl），坏了一块文件系统就 backup 一个 `.db` 文件。

---

## 1.4 数据流（一次完整扫描）

```
T+0s    systemd timer 触发 fliggy-monitor.service
T+0.1s  加载 SQLite 静态数据 (pois, sellers, config)
T+0.2s  加载 /etc/fliggy-vps/cookies.json
T+0.3s  对每个 enabled POI:
          - 调 MtopClient.shelf(poiId)                ~250ms
          - parse_ticket_cells → list[cell]
          - sleep(random(0.2, 0.8))                    限流抖动
T+5s     8 POI 全扫完，开始 diff vs 上一轮:
          - 新出现的 sellerId → 触发 booktips 补齐
          - 非自营 cell 集合 → upsert 到 cells_snapshot
          - 写入本轮 round 记录
T+8s     对本轮每个非自营 cell:
          - 触发 webhook（带去重 key: sha1(poiId+itemId+skuId+sellerId)）
T+10s    收尾，本轮 summary 写日志
T+10s    sleep(30 min - elapsed)
T+30min  下一轮
```

---

## 1.5 部署目标

| 项 | 值 |
|---|---|
| 公网 IP | 107.172.144.102 |
| SSH | root@107.172.144.102:22 |
| 二级域名 | feizhu.19880913.xyz → 107.172.144.102（A 记录） |
| TLS | Let's Encrypt（自动续期，Caddy 处理） |
| 监听 | `:443` (Caddy) → `127.0.0.1:8080` (Uvicorn) |
| 数据目录 | `/opt/fliggy-monitor/{code,data,docs,web,scripts,logs}` |
| 配置目录 | `/etc/fliggy-monitor/cookies.json`（chmod 600） |
| 系统用户 | `monitor`（uid 1001，nologin） |
| 工作目录 | `/opt/fliggy-monitor` |

---

## 1.6 安全模型

| 威胁 | 缓解 |
|---|---|
| 公网暴露 dashboard 被扫 | 浏览器单密码 + Cookie HttpOnly + SameSite=Strict + 失败 5 次锁 10 min |
| 登录密码明文落盘 | bcrypt hash（cost=12）存 SQLite config 表 |
| Webhook URL 泄露 | 不在 URL 里带 token；URL 本身是 secret |
| Cookies 泄露 | chmod 600 / owner root、systemd `ProtectSystem=strict` |
| VPS 被入侵横向 | monitor 用户 nologin、`NoNewPrivileges=true`、`PrivateTmp=true` |
| 反爬被风控 | UA 固定、间隔抖动、cookie 自动续期、不上多线程并发 |

---

## 1.7 鉴权与 Session（专门一节）

> **明确：本项目用 Session-based auth（cookie + bcrypt + 服务端 session store），**不**用 HTTP Basic、**不**用 OAuth、**不**用 JWT。** 原因：单用户内部工具，Session 实现最简单、最稳、和浏览器天然集成。

#### 鉴权流程

```
┌────────┐                              ┌──────────────┐                  ┌──────────────┐
│ 浏览器  │                              │  Caddy 443   │                  │ Uvicorn 8080 │
└───┬────┘                              └──────┬───────┘                  └──────┬───────┘
    │  GET /login  (no session)               │                                  │
    │ ───────────────────────────────────────►│ ───────────────────────────────► │
    │ ◄───────────────────────────────────────│ ◄─── 200 HTML (登录页) ───────── │
    │                                                                            │
    │  POST /login  {password: "xuran888"}                                       │
    │ ─────────────────────────────────────────────────────────────────────────► │
    │                                          1. 查 config.login_locked_until    │
    │                                          2. bcrypt.checkpw(pwd, hash)      │
    │                                          3. 成功 → 生成 sid + 写 session   │
    │                                          4. Set-Cookie: sentinel_session=… │
    │ ◄───────────────────────────────────────────────────────────────────────── │
    │  302 /                                                                     
    │  GET /  Cookie: sentinel_session=abc123                                      │
    │ ─────────────────────────────────────────────────────────────────────────► │
    │                                          查 sid → 命中 → 渲染 dashboard     │
    │ ◄───────────────────────────────────────────────────────────────────────── │
```

#### Session 存储

**服务端 session 用 SQLite 单独一张表 `web_sessions`**（见 §1.7 的 DDL）：

```sql
CREATE TABLE web_sessions (
    sid         TEXT PRIMARY KEY,            -- 32-char URL-safe random（secrets.token_urlsafe(32)）
    created_at  TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,               -- created_at + 7 days
    user_agent  TEXT,                        -- 仅记浏览器 family（Chrome/Safari/Firefox），不存完整 UA
    ip_prefix   TEXT,                        -- /24 段（前三段），不存完整 IP（隐私 + 防日志泄露）
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_web_sessions_expires ON web_sessions(expires_at);
CREATE INDEX idx_web_sessions_active  ON web_sessions(is_active);
```

> **为什么不用纯 cookie 存 session**：把 sid 直接当 cookie 是不行的——任意伪造 → 任意登录。必须 sid 是「服务端随机串」，校验时查表确认 sid 存在且 `is_active=1` 且 `expires_at > now`。

#### Cookie 规格

```
Set-Cookie: sentinel_session=<sid>; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800
```

| 属性 | 值 | 理由 |
|---|---|---|
| `HttpOnly` | ✅ | 阻止 JS 读，防 XSS |
| `Secure` | ✅ | 仅 HTTPS（Caddy 终止 TLS） |
| `SameSite=Strict` | ✅ | 防 CSRF，dashboard 不跨域 |
| `Path=/` | ✅ | 所有路径都带 |
| `Max-Age=604800` | 7 天 | 不需要"长期登录"——内部工具 |
| `Domain` | 不设 | 默认 = feizhu.19880913.xyz，不给子域 |

#### 密码

| 项 | 规格 |
|---|---|
| 算法 | bcrypt（`bcrypt>=4.0`，cost=12） |
| 存储 | `config.admin_password_hash`（JSON 字符串，JSON-encoded） |
| 默认密码 | `xuran888`（首次部署由 `init_db.py` 写入） |
| 修改密码 | dashboard `/settings` → 输入旧/新/新重复 → 重 hash |
| 密码强度 | 服务端 ≤ 72 bytes（bcrypt 限制）；前端 maxlength=72 |
| 明文密码 | **从不**写日志、从不进 cookie、从不进 URL、从不进 webhook payload |

#### 失败计数 / IP 锁定

```sql
CREATE TABLE login_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_prefix   TEXT NOT NULL,               -- /24 段
    ts          TEXT NOT NULL,
    user_agent  TEXT
);
CREATE INDEX idx_login_fail_ip ON login_failures(ip_prefix, ts DESC);
```

**判定逻辑**（每次 POST /login）：
```python
recent = SELECT COUNT(*) FROM login_failures
         WHERE ip_prefix=? AND ts > datetime('now', '-10 minutes');
if recent >= 5:
    return 429  # locked
```

**每次失败**：INSERT 一行（保留 24h 后 cleanup）。
**每次成功**：DELETE 该 ip_prefix 全部旧记录。

#### Session 生命周期

| 事件 | 处理 |
|---|---|
| 创建（POST /login 成功） | INSERT sid, expires_at = now + 7d |
| 请求时 | UPDATE last_seen_at, 检查 expires_at, 检查 is_active |
| POST /logout | UPDATE is_active=0（保留行用于审计 7 天后清理） |
| 过期（expires_at < now） | 中间件拒绝，下一次 GET 自动重定向 /login |
| 滑动续期 | 每次成功请求如果 last_seen_at < now - 1h，续 expires_at = now + 7d（避免长时间盯屏突然掉登录） |
| 改密码 | UPDATE is_active=0 该用户全部 session（强制重新登录） |

#### Auth 中间件（FastAPI）

```python
# web/auth.py（节选）
from fastapi import Request, HTTPException, Depends
from fastapi.responses import RedirectResponse

PUBLIC_PATHS = {"/login", "/healthz", "/static"}
SESSION_COOKIE = "sentinel_session"
SESSION_TTL_SEC = 7 * 86400

async def require_session(request: Request) -> str:
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return None
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="not authenticated")
    sess = db.get_active_session(sid)
    if not sess:
        raise HTTPException(status_code=401, detail="session expired")
    request.state.sid = sid
    return sid

# 全局依赖：所有非公开路由都要 require_session
# main app 加 middleware 把 401 → 重定向到 /login?next=<原路径>
```

#### 前端 logout 按钮

```html
<form method="POST" action="/logout" style="display:inline">
    <button type="submit" class="btn-link">登出</button>
</form>
```

#### 攻击面 & 缓解

| 威胁 | 缓解 |
|---|---|
| 暴力破解密码 | bcrypt 故意慢（~250ms）+ IP /24 段 10 min 锁定 5 次失败 |
| Session 伪造 | sid 是 `secrets.token_urlsafe(32)`（256 bit 熵）+ 服务端校验 |
| Session 劫持 | `HttpOnly` 阻止 JS 读 + `Secure` 仅 HTTPS + `SameSite=Strict` |
| CSRF | `SameSite=Strict` + 所有改状态操作 POST + 登录后页面 referer 检查 |
| XSS 偷 cookie | `HttpOnly` + CSP `default-src 'self'` |
| 登录后长期不退出 | 7 天硬过期 + 滑动续期 |
| 多设备登录 | **允许多 session**（不限设备数）；改密码时清全部 |
| 日志泄漏 sid | access log 过滤 `Cookie:` header（Caddy 配置） |
| 数据库泄漏含明文密码 | bcrypt cost=12，单条密码解不开（即便 DB 全裸） |

#### 与监控脚本的关系

- **监控脚本不需要登录态**——它读 `/etc/fliggy-monitor/cookies.json`（飞猪 mtop cookie），**不是 dashboard session**。两套完全独立
- 监控脚本写 SQLite（cells_snapshot / alerts / rounds），web 读 SQLite；二者通过 DB 解耦，不需要共享任何 auth state

#### 与 webhook 的关系

- webhook URL 和 secret 存 `config` 表，**所有 web 路由可读**（dashboard 改设置需要读）；不在 webhook URL 里带 auth token（签名校验用 HMAC）
- webhook 是出站调用，**不需要** dashboard session

---

## 1.8 数据模型：POI → 商品类型 → 货架（三层）

> 这一节把 handoff 的「shelf cell」概念正式化为产品语言：**POI → 商品类型(cell_type) → 货架(cell)**。最小单位是 **货架（cell）**，每个货架挂一个 seller。

```
┌─────────────────────────────────────────────────────────────┐
│  POI（景区）                                                  │
│   圆明园 (poiId=1345)                                         │
│                                                              │
│   ├─ 商品类型: 景点门票      ── 来自 shelf "景点门票"          │
│   │    ├─ 货架 #1  itemId=994832029673 skuId=5976452104959    │
│   │    │      "成人票"     seller=宫足迹旅行社旗舰店 ¥16     │
│   │    ├─ 货架 #2  itemId=994832029673 skuId=5976452104963    │
│   │    │      "儿童票"     seller=宫足迹旅行社旗舰店 ¥8      │
│   │    └─ 货架 #3  itemId=994832029673 skuId=6120965321453    │
│   │           "学生票"     seller=宫足迹旅行社旗舰店 ¥8      │
│   │                                                            │
│   ├─ 商品类型: 门票套餐      ── 来自 shelf "门票套餐"          │
│   │    ├─ 货架 #4  itemId=1065739764221 skuId=6276363111198    │
│   │    │      "大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解"  │
│   │    │      seller=宫足迹旅行社旗舰店 ¥31                     │
│   │    └─ 货架 #5  itemId=1063764674916 skuId=6126605864742    │
│   │           "大门票+电子导览"  seller=宫足迹旅行社旗舰店 ¥18 │
│   │                                                            │
│   └─ 商品类型: 园内项目      ── 来自 shelf "园内项目"          │
│        └─ 货架 #6  itemId=771165079110 skuId=5452191778939     │
│             "圆明园(电子导览)+清华大学(电子导览)"              │
│             seller=南宁哪都通旅游专营店 ¥10                     │
└─────────────────────────────────────────────────────────────┘
```

#### 三层语义

| 层 | 对应 handoff 字段 | 数量级（每 POI） |
|---|---|---|
| **POI** | `poi_registry.json` `pool[]` | 1 |
| **商品类型** (cell_type) | `cell.cellType`（shelf `name`） | 3-6 种 |
| **货架** (cell) | `cells[]` & `tabs[].cells[]`（每 cell 一条） | 5-10 个 |

#### 商品类型枚举（实测 8 POI）

| cell_type | 含义 | 出现频次 |
|---|---|---|
| `景点门票` | 成人/儿童/学生等基础票 | 高 |
| `门票套餐` | 大门票 + 附加项（讲解/导览/联票） | 高 |
| `园内项目` | 园内子项目（电瓶车/演出/讲解器） | 中 |
| `景区联票` | 多景区联票 | 中 |
| `周边景区门票` | 跨 POI 单门票 | 少（仅北京动物园等） |
| `周边景区套票` | 跨 POI 套票 | 少 |

> 数据中 cell.poiId **可能不等于**当前 POI 的 poiId（跨 POI 套票）。前端展示 cell.poiName 而不是 POI 级的 poiName。详见 [02-frontend-design.md §2.X](02-frontend-design.md)。

#### Seller 三档角色（前端用颜色区分）

| 角色 | 数据来源 | 视觉标识 | 触发 |
|---|---|---|---|
| **自营** | `seller.seller_id == SELF_SELLER_ID` | 左色条 `--phosphor` + `[SELF]` 标签 | 自动 |
| **关注** | `seller_enrichment.is_watched = 1` | 左色条 `--cyan` + `★` 前缀 | 用户手动 |
| **其他** | 其他 seller | 无色条 + sellerId 或 seller_name | 默认 |

> 新增 `seller_enrichment` 表（[03-database-schema.md §3.2.X](03-database-schema.md)），存用户补充的 display_name / is_watched / notes / tags。

#### 名称回退规则（重要）

货架行的 seller 列展示优先级：

```
1. seller_enrichment.display_name  ← 用户手动覆盖（最高优先级）
2. sellers.seller_name             ← booktips 自动拉的（已有 16 个）
3. sellerId 前 6 位 + "…"          ← 兜底：221759…（用户可点进去补全）
```

第三档「兜底显示 ID」是 v1 关键——监控发现新 sellerId 时立即入库（sellers 表 + 触发 booktips），但 booktips 可能失败/超时/字段缺失。前端允许用户从「兜底行」一键跳到 `/sellers/{id}` 编辑 enrichment。

---

## 1.9 GitLab 部署流程（私有仓 + CI/CD）

> 本节定义从开发 → 推送到 GitLab → 自动部署到 VPS 的全链路。**前提**：你的 GitLab 是私有部署（自托管 / GitLab.com 私有仓均可）。

#### 仓库结构

```
git@<your-gitlab>:<username>/fliggy-monitor.git     ← 私有仓（private）

分支策略:
  main            ← 受保护；只接受 merge request；CI/CD 自动部署到生产
  feat/*          ← 新功能开发；merge → main
  fix/*           ← bug 修复
  hotfix/*        ← 紧急修复；可直推 main（需 maintainer）
  tags (v*)       ← 触发版本归档 + 部署
```

#### CI/CD 阶段（`.gitlab-ci.yml`）

```yaml
stages:
  - lint
  - test
  - build
  - deploy

# 1. lint：Python 语法 + import 健康
lint:
  stage: lint
  image: python:3.12-slim
  script:
    - python -m py_compile $(find code web scripts -name '*.py')
    - python -c "import ast; [ast.parse(open(p).read()) for p in __import__('glob').glob('**/*.py', recursive=True)]"

# 2. test：单元测试 + 冒烟（mock mtop，不真打飞猪）
test:
  stage: test
  image: python:3.12-slim
  before_script:
    - pip install -r requirements-test.txt
  script:
    - pytest tests/ -v --cov=web --cov=code

# 3. build：打包源码为 tar.gz 存 GitLab Package Registry
build:
  stage: build
  image: alpine:3.19
  script:
    - apk add --no-cache tar gzip
    - tar czf fliggy-monitor-${CI_COMMIT_SHORT_SHA}.tar.gz
        --exclude='*.pyc' --exclude='__pycache__'
        --exclude='.git' --exclude='*.db' --exclude='backups/*.db'
        code/ data/ web/ scripts/ docs/ deploy/ README.md requirements.txt
    - echo "build artifact: $(ls -lh fliggy-monitor-*.tar.gz)"
  artifacts:
    paths:
      - fliggy-monitor-*.tar.gz
    expire_in: 7 days

# 4. deploy：仅 main 分支触发；SSH 到 VPS 拉取最新 + 重启服务
deploy_production:
  stage: deploy
  image: alpine:3.19
  before_script:
    - apk add --no-cache openssh-client rsync
    - mkdir -p ~/.ssh && echo "$DEPLOY_SSH_KEY" > ~/.ssh/id_ed25519
    - chmod 600 ~/.ssh/id_ed25519
    - ssh-keyscan -H "$VPS_HOST" >> ~/.ssh/known_hosts 2>/dev/null
  script:
    - rsync -avz --delete
        --exclude='.git' --exclude='*.db' --exclude='backups/*.db'
        --exclude='cookies.json' --exclude='__pycache__'
        ./ monitor@$VPS_HOST:/opt/fliggy-monitor/
    - ssh monitor@$VPS_HOST << 'REMOTE'
        cd /opt/fliggy-monitor
        sudo -u monitor .venv/bin/pip install -r requirements.txt --quiet
        sudo systemctl restart fliggy-web
        sudo systemctl restart fliggy-monitor.timer || true
        echo "deploy done: $(date)"
      REMOTE
  environment:
    name: production
    url: https://feizhu.19880913.xyz
  only:
    - main
    - tags
  when: manual    # 默认手动点按钮，避免误推
```

#### GitLab 仓库的 CI/CD Variables（在 GitLab UI 配）

| Variable | 类型 | 说明 |
|---|---|---|
| `VPS_HOST` | masked variable | `107.172.144.102` |
| `DEPLOY_SSH_KEY` | file / masked | 专用于 deploy 的 ed25519 私钥（**不要用 root 密码**，用 SSH key） |
| `DEPLOY_KNOWN_HOSTS` | file | `VPS_HOST` 的 ssh-keyscan 输出 |

**关键安全**：
- GitLab Runner **不**直接用 root + 密码 deploy。流程：本地 `ssh-keygen` 生成 ed25519 密钥对 → 公钥加到 VPS `/home/monitor/.ssh/authorized_keys`（chmod 600, owner monitor）→ 私钥存 GitLab Variable
- deploy 用 `monitor` 用户（不是 root）；systemctl 重启用 sudo 限定命令（`/etc/sudoers.d/monitor-systemctl`）
- cookies.json 不进 git，deploy 排除；本地和 VPS 各维护一份

#### `monitor` 用户的 sudo 限定

```bash
# 在 VPS 上
echo "monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart fliggy-*" \
    > /etc/sudoers.d/monitor-systemctl
echo "monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl start fliggy-*" \
    >> /etc/sudoers.d/monitor-systemctl
echo "monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop fliggy-*" \
    >> /etc/sudoers.d/monitor-systemctl
chmod 440 /etc/sudoers.d/monitor-systemctl
```

#### 本地开发循环

```bash
# 本机
cd /Users/argo/666-XCJ/fliggy-monitor
git remote add origin git@<gitlab>:<user>/fliggy-monitor.git

# 改完代码
git add -A
git commit -m "feat: 加 watched seller 高亮"
git push origin feat/watched-highlight

# GitLab UI 创建 Merge Request → 触发 lint + test
# 合并到 main → 手动点 deploy_production → 自动部署到 VPS
```

#### 首次部署（把现有代码推到 GitLab）

```bash
# 1. 在 GitLab UI 创建空项目 fliggy-monitor（private）

# 2. 本机初始化
cd /Users/argo/666-XCJ/fliggy-monitor
git init
git add -A
git commit -m "init: phase 1 design docs + handoff code"
git remote add origin git@<gitlab>:<user>/fliggy-monitor.git
git push -u origin main

# 3. CI/CD Variables 配置（GitLab UI → Settings → CI/CD → Variables）

# 4. VPS 上创建 monitor 用户 + 部署密钥（一次性，详见 05-deployment-vps.md §5.3）

# 5. 在 GitLab UI 第一次点 "deploy_production"
```

#### Rollback（紧急回滚）

```bash
# 在 GitLab UI → Pipelines → 选上一个 successful pipeline → 点 "Run again"
# 或者直接:
ssh monitor@107.172.144.102 \
    'cd /opt/fliggy-monitor && git checkout HEAD~1 -- code web scripts && \
     sudo systemctl restart fliggy-web && \
     sudo systemctl restart fliggy-monitor.timer'
```

#### 详见

- [.gitlab-ci.yml 完整版](08-gitlab-deploy.md) — 文档 08 详述
- [VPS 部署](05-deployment-vps.md) — VPS 一次性配置

---

## 1.10 文件组织

```
fliggy-monitor/
├── README.md                          ← 项目入口
├── docs/                              ← 设计文档（你正在读的这套）
│   ├── 01-architecture.md             ← 本文件
│   ├── 02-frontend-design.md          ← 前端设计
│   ├── 03-database-schema.md          ← DB schema
│   ├── 04-webhook-spec.md             ← webhook 规范
│   ├── 05-deployment-vps.md           ← VPS 部署
│   ├── 06-polling-frequency.md        ← 30 min 频率策略
│   └── 07-operation-runbook.md        ← 运维手册
├── code/                              ← 来自 fliggy-vps-handover/code/
│   ├── selectors.py
│   ├── mtop_client.py
│   └── fliggy_monitor.py              ← 增强版（写 DB + 触发 webhook）
├── data/                              ← 来自 fliggy-vps-handover/data/
│   ├── poi_registry.json
│   ├── seller_baseline.json
│   ├── seller_cache.json
│   └── monitor.db                     ← SQLite（运行时生成）
├── web/                               ← 新增
│   ├── server.py                      ← FastAPI app
│   ├── auth.py                        ← 密码 / session
│   ├── db.py                          ← SQLite 封装
│   ├── routes/
│   │   ├── pages.py                   ← HTML 路由
│   │   └── api.py                     ← JSON API
│   ├── templates/                     ← Jinja2
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── dashboard.html             ← 主面板
│   │   ├── poi_detail.html
│   │   ├── sku_detail.html
│   │   ├── alerts.html
│   │   ├── settings.html
│   │   └── partials/
│   │       ├── _sku_row.html
│   │       ├── _alert_item.html
│   │       └── _poi_card.html
│   └── static/
│       ├── css/main.css               ← design tokens + 布局
│       ├── js/app.js                   ← Alpine.js / HTMX setup
│       └── fonts/                     ← JetBrains Mono / IBM Plex Sans
├── scripts/
│   ├── init_db.py                     ← 建表 / 导入 JSON 数据
│   ├── deploy.sh                      ← 一键部署脚本
│   ├── refresh_cookies.py             ← playwright cookie 续期
│   └── verify_sign.py                 ← sign 算法验证（来自 handoff）
├── tests/
│   ├── smoke.py                       ← 来自 handoff
│   ├── test_db.py                     ← 新增
│   └── test_webhook.py                ← 新增
├── deploy/
│   ├── Caddyfile
│   ├── systemd/
│   │   ├── fliggy-web.service
│   │   ├── fliggy-monitor.service
│   │   ├── fliggy-cookies-refresh.service
│   │   └── fliggy-cookies-refresh.timer
│   ├── logrotate.d-fliggy-monitor
│   └── init-monitor.sh                ← 首次部署
└── fliggy-vps-handover/               ← 原始 handoff 包（保留）
```

---

## 1.11 接下来

1. **本轮（已完成）**：6 份设计文档
2. **下一轮**：实现 web/server.py + templates + static/css 骨架；增强 fliggy_monitor.py 写 DB
3. **再下一轮**：部署到 VPS + 注入 cookies + 跑通端到端 + 截图验证前端

详见每份文档的具体 spec。