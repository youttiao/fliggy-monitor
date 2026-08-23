# 飞猪哨兵 · Fliggy Sentinel

> 一个**内部运营 dashboard** + **后台采集 + 告警**工具，用于监控飞猪 H5 上 8 个北京景区的门票 SKU，找出非自营的外部商家（含「关注的供应商」标记），并通过 webhook 把告警推到你的 IM（飞书 / 钉钉）。

---

## 文档索引（按顺序读）

| # | 文档 | 内容 |
|---|---|---|
| 1 | [01-architecture.md](docs/01-architecture.md) | 技术栈、数据流、安全模型、Session 鉴权、数据模型（POI→商品类型→货架）、GitHub 部署 |
| 2 | [02-frontend-design.md](docs/02-frontend-design.md) | 美学方向、design tokens、7 个页面的 ASCII 线框、3 档角色色、HTMX/Alpine 用法 |
| 3 | [03-database-schema.md](docs/03-database-schema.md) | SQLite 10 张表（含 web_sessions / seller_enrichment）、查询示例、保留策略 |
| 4 | [04-webhook-spec.md](docs/04-webhook-spec.md) | 6 种告警 Payload、签名算法、钉钉/企微/飞书/Bark/Telegram 适配 |
| 5 | [05-deployment-vps.md](docs/05-deployment-vps.md) | 107.172.144.102 上线步骤、systemd 4 unit、Caddyfile |
| 6 | [06-polling-frequency.md](docs/06-polling-frequency.md) | 30 min 调度策略、抖动、续期配合 |
| 7 | [07-operation-runbook.md](docs/07-operation-runbook.md) | 日常巡检、故障 Q&A、备份恢复 |
| 8 | [08-github-deploy.md](docs/08-github-deploy.md) | GitHub 私有仓 + Actions CI/CD 完整 pipeline、密钥、cookie 隔离 |
| 9 | [09-seller-enrichment.md](docs/09-seller-enrichment.md) | 关注的供应商 + 人工补全 display_name 流程 |

---

## 数据模型（三层语义）

```
POI（景区）
  └─ 商品类型（cell_type：门票套餐/景点门票/园内项目/景区联票/...）
       └─ 货架（cell = itemId + skuId，每个挂一个 seller）
```

最小单位是**货架（cell）**。每个货架有 3 档角色标识：

| 角色 | 数据来源 | 视觉 | 操作 |
|---|---|---|---|
| **自营** | seller_id == `2217592322543` | 磷光左色条 + `[SELF]` | 系统自动 |
| **关注** | `seller_enrichment.is_watched = 1` | 青色左色条 + `★` | 用户手动 |
| **其他** | 默认 | 无色条 + sellerId 兜底 | — |

seller 显示名三档优先级：**用户覆盖** > **booktips 自动** > **ID 截断**（"221759…"）。

---

## 原始 Handoff（继承自 `fliggy-vps-handover/`）

| 类别 | 文件 | 来源 |
|---|---|---|
| API 常量 | `code/selectors.py` | fliggy-vps-handover |
| mtop client | `code/mtop_client.py` | fliggy-vps-handover |
| 监控模板 | `code/fliggy_monitor.py` | fliggy-vps-handover（增强版：写 DB + 告警） |
| POI 池 | `data/poi_registry.json` | fliggy-vps-handover（8 POI） |
| 自营基线 | `data/seller_baseline.json` | fliggy-vps-handover |
| 卖家缓存 | `data/seller_cache.json` | fliggy-vps-handover（16 sellers，作为初始 baseline） |
| 文档 | `fliggy-vps-handover/docs/*` | fliggy-vps-handover（参考） |
| 冒烟测试 | `tests/smoke.py` | fliggy-vps-handover |

---

## 一句话目标

让运营方在 `https://feizhu.19880913.xyz` 浏览器里，**5 秒回答"现在哪个 POI 有非自营 SKU 在卖 / 我关注的对手在卖什么"**，并在出现新对手 / 自营缺位 / 价格异动时通过 webhook 实时推送到自己的 IM。

---

## 关键设计选择

| 决策 | 选择 | 一句话理由 |
|---|---|---|
| Web 框架 | FastAPI + Jinja2 | 类型清晰、单文件起、无构建步骤 |
| 前端 | HTMX + Alpine.js + 手写 CSS | 内部工具不需要 SPA；CSS 用 design tokens |
| 数据库 | SQLite WAL | 单机零运维 |
| 反代 | Caddy | 自动 HTTPS + Let's Encrypt 续期 |
| 调度 | systemd timer | 系统级、可观察、可持久化 |
| 部署 | GitHub Actions + 私有仓 | 推送即部署，密钥通过 Secrets 隔离 |
| 前端色板 | 暗色 / 磷光+青+琥珀（3 档） | Bloomberg terminal / Linear 内部工具感 |
| 主表字体 | 等宽（JetBrains Mono） | 高信息密度、列严格对齐 |
| 监控频率 | 30 min/POI | 业务时效够 + 反爬无压力 |
| 鉴权 | Session + 服务端 store + 失败锁定 | 单用户明确不用 Basic / OAuth / JWT |
| 登录密码 | 后端硬编码 `xuran888`（v1） | 用户要求先不上 bcrypt |
| Webhook | 飞书 / 钉钉（v1） | 用户指定 |
| 告警去重 | 1 h 窗口 | 避免刷屏 |

---

## 部署速览（GitHub Actions 流程）

```bash
# 本机（一次）
cd /Users/argo/666-XCJ/fliggy-monitor
git init -b main && git add -A && git commit -m "init"
# GitHub UI: 创建私有仓 666-XCJ/fliggy-monitor
git remote add origin git@github.com:666-XCJ/fliggy-monitor.git
git push -u origin main

# GitHub UI：
#   - Settings → Secrets → Actions 配 4 个（VPS_HOST, VPS_USER, VPS_SSH_KEY, VPS_PORT）
#   - 推送后 Actions 自动跑 lint + test + build
#   - 手动 Run workflow → deploy_production job → SSH 到 VPS 重启服务
```

详见 [08-github-deploy.md](docs/08-github-deploy.md) 和 [05-deployment-vps.md](docs/05-deployment-vps.md)。

---

## 实现路线图

- [x] **Phase 1**：9 份设计文档
- [x] **Phase 2**：web 骨架（server / auth / db / templates / CSS / JS）
- [x] **Phase 3**：增强 `code/fliggy_monitor.py`（写 SQLite + 生成 alerts + 推 webhook）
- [x] **Phase 4**：脚本（init_db / refresh_cookies / deploy）+ GitHub Actions workflow
- [ ] **Phase 5**：本机 uvicorn + 假数据 + 截图验证前端
- [ ] **Phase 6**：GitHub Actions 部署到 107.172.144.102 + 注入飞猪 cookies + 端到端联调
- [ ] **Phase 7**：正式上线运营

---

## 默认仓库路径：`666-XCJ/fliggy-monitor`

项目仓库按本地目录命名取 `666-XCJ/fliggy-monitor`（Private）。需要改 owner / 名字请编辑 `.github/workflows/deploy.yml` 与本文档。

---

## 状态

**当前**：Phase 2-4 完成（web 骨架 + 监控脚本增强 + GitHub Actions 流水线）。

**下一步**：Phase 5 — 本机 uvicorn 起服务，用 SQLite 假数据驱动 dashboard 并截图验证前端。