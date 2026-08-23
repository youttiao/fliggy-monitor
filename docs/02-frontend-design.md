# 02 · 前端设计（飞猪哨兵 Dashboard）

> 受众：单一中文运营用户（你本人）。任务：5 秒回答"现在哪个 POI 有非自营 SKU 在卖"，下钻看完整 seller 信息和历史告警，配置 webhook 和监控 POI。

---

## 2.1 美学方向（带风险的选择）

**核心隐喻**：情报控制台 / 操作员工作台。长时间盯屏、信息密度高、单色调为主、一个告警色。

**和 AI 默认的距离**：
- ❌ 不用"奶油色背景 + 衬线大字 + 陶土色"（生成器最爱 #1）
- ❌ 不用"近黑 + 单一霓虹绿/朱红"（生成器最爱 #2，闪光弹感）
- ❌ 不用"宽幅报纸 + 极细分隔线 + 多栏新闻"（生成器最爱 #3）
- ✅ 用"近黑 + 冷青色（phosphor-cyan）+ 琥珀色告警（amber）"——但饱和度都压低，靠 hairline 灰线和字号对比撑信息密度

**和"用户要 Bloomberg terminal / Linear 感"的契合**：Linear 是内部 ops 工具的代表——深色、等宽数字、几乎没用阴影，全靠 hairline rule + 字号 + 颜色三件套分清层级。我们沿这条路走，把风险押在一个选择上：

> **整张 SKU 主表用等宽字体显示**——不只是数字，连 SKU 名称、店铺名都用 mono。这是和"消费级网站"最远的距离，也最贴"机器在读的表"。每一列对齐像代码一样精确。

---

## 2.2 Design Tokens

#### 色彩（CSS 自定义属性）

```css
:root {
  /* ── Surface ─────────────────────────────────────── */
  --ink:        #0B0E14;   /* 页面底色，冷黑偏蓝 */
  --panel:      #13171F;   /* 卡片 / 行 */
  --panel-up:   #1A2030;   /* 悬停 / 选中 / 焦点 */

  /* ── Rule & Border ─────────────────────────────── */
  --rule:       #262C36;   /* hairline 分隔线 1px */
  --rule-bold:  #3A414E;   /* 强调分隔线（表头下、模块之间） */

  /* ── Text ───────────────────────────────────────── */
  --text:       #D4D7DD;   /* 正文 */
  --text-dim:   #7A8290;   /* 次要 / 占位 */
  --text-faint: #4A5260;   /* 标签 uppercase 颜色 */

  /* ── Functional（三档）────────────────────────── */
  --phosphor:   #4FD0B8;   /* 自营（LEFT bar + SELF badge + live 指示器 + 链接） */
  --cyan:       #5EAEFF;   /* 关注（LEFT bar + ★ badge）—— 与 phosphor 错开饱和度 */
  --amber:      #E5A847;   /* 非自营新出现 / 告警 / 价格异动 —— 状态变更用色，不是行色 */

  /* ── Misc ───────────────────────────────────────── */
  --ok:         #7BC97B;   /* 成功态（克制绿；与 phosphor 错开；仅 webhook sent 等单点） */
  --shadow:     0 1px 0 rgba(255,255,255,0.02);  /* 极弱阴影，几乎不可见 */
}
```

**为什么不是单色磷光**：纯 phosphor 绿 + 黑底像 80 年代终端，复古但易疲劳。把"自营"从 phosphor 里独立出来作为单色 token，把"正常 / 在线"也归到 phosphor——一套色调承担两个语义，是控制台常用的精炼手法。

**为什么琥珀而不是红**：红色 = 致命错误。监控 SKU 是日常运营，不是 911 告警；琥珀色更温和、更适合长时间盯屏。

**为什么再加一个 cyan 给"关注"**：原方案只用 phosphor/amber 两色，新加"关注"维度（用户在 seller_enrichment 表手动标记）后变成三档：
- **phosphor** = **角色标识**（我是谁）
- **cyan** = **角色标识**（我盯着谁）
- **amber** = **事件标识**（这条刚刚发生了异常）

amber 不再作行色——避免「关注的人」和「新出现的人」都被琥珀高亮导致噪音。amber 现在只在告警灯、状态变更、`first_seller` 新出现等**一次性事件**用，且**24h 后褪色**回默认色。

#### 字体

```css
:root {
  --font-display: 'IBM Plex Sans Condensed', 'Helvetica Neue', sans-serif;  /* 极少用，大标题 */
  --font-body:    'IBM Plex Sans', system-ui, -apple-system, sans-serif;    /* UI 文字 */
  --font-mono:    'JetBrains Mono', 'SF Mono', Menlo, monospace;            /* 数据 */
  --font-num:     var(--font-mono);  /* 数字 = mono，永远 */

  --fz-xs:  11px;   /* caption / 标签 uppercase tracked */
  --fz-sm:  12px;   /* 表格内、SKU 名 */
  --fz-md:  13px;   /* 表头、列表 */
  --fz-lg:  15px;   /* 正文 */
  --fz-xl:  20px;   /* 大数字（计数） */
  --fz-xxl: 28px;   /* 仅 hero */
}
```

**字体引入**：self-host JetBrains Mono (Regular 400 + Medium 500) 和 IBM Plex Sans (Regular 400 + Medium 500 + SemiBold 600)。仅两个 family，每个 3 个字重 = 6 个 woff2 文件 ≈ 240 KB。**全部 woff2 子集化**（用 fontTools subset to latin + cjk-common）。

#### 间距 / 圆角

```css
:root {
  --sp-1:  4px;
  --sp-2:  8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-5: 24px;
  --sp-6: 32px;
  --sp-8: 48px;

  --radius-sm: 2px;   /* 几乎不圆角——内部工具美学 */
  --radius-md: 4px;   /* 仅按钮、输入框 */
  --radius-lg: 0;     /* 卡片不圆 */
}
```

#### 动效

- **时钟 tick**：顶部状态条 mono 时钟每 1s 字符"跳动"（CSS `@keyframes` opacity 微闪，60ms）
- **扫描完成脉冲**：扫描状态从 amber 短暂闪 200ms 后回 phosphor
- **行进入**：新告警从右滑入 8px，180ms
- **Hover**：行底色 `--panel` → `--panel-up`，80ms ease-out
- **不**做：页面加载大动画、scroll-triggered reveal、ambient gradient。运营工具，最怕注意力被抢

#### 减弱动效偏好

```css
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
```

---

## 2.3 信息架构

### 路由

| 路由 | 用途 | 鉴权 |
|---|---|---|
| `GET /login` | 登录页（密码输入） | 公开 |
| `POST /login` | 校验密码，写 session cookie | 公开 |
| `POST /logout` | 清 session | 登录 |
| `GET /` | Dashboard 总览（POI 卡片 + 最新一轮摘要） | 登录 |
| `GET /poi/{poi_id}` | 单 POI 详情（按 cell_type 分组 + 筛选栏） | 登录 |
| `GET /sku/{item_id}/{sku_id}` | 单 SKU 详情（booktips 原始 + 历史价） | 登录 |
| `GET /sellers` | 卖家管理（列表 + 筛选） | 登录 |
| `GET /sellers/{seller_id}` | 单 seller 详情 + 编辑 display_name / 关注 | 登录 |
| `POST /sellers/{seller_id}` | 保存 seller_enrichment 编辑（表单提交） | 登录 |
| `POST /api/sellers/{seller_id}/watch` | 切换关注状态（AJAX 乐观更新） | 登录 |
| `GET /alerts` | 告警历史（分页 + 过滤） | 登录 |
| `GET /settings` | 设置（webhook、POI 启停、密码修改） | 登录 |
| `GET /api/rounds` | JSON: 轮询历史（HTMX 拉数据用） | 登录 |
| `GET /api/cells?poi=X` | JSON: 当前某 POI 的 SKU 列表（HTMX 拉新） | 登录 |
| `POST /api/config` | 改设置 | 登录 |

### 单页 dashboard 的五个"面"

整个工具是**单 SPA 风格的页面栈**（但服务端渲染）：

1. **顶部状态条（sticky，56px）**
2. **顶部下方告警 tape（28px）**
3. **POI 卡片网格（主区）**
4. **点击 POI → 抽屉式 SKU 表（按 cell_type 分组）**
5. **设置 / 卖家管理（独立路由，深链）**

---

## 2.4 ASCII 线框

### 2.4.1 Dashboard（`/` 路由）

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 飞猪哨兵  ● LIVE  14:23:07  上次扫描: 14:00 (23 min ago)  POI 8/8  SKU 60  ⚠ 16   │  ← 56px 顶栏
├──────────────────────────────────────────────────────────────────────────────────┤
│ ⚠ 14:00  圆明园  「大门票+西洋楼」  宫足迹旅行社旗舰店     ¥58  详情 →            │  ← 28px 告警 tape
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐                │
│  │  圆明园   ⚠ 6/6  │  │ 天坛公园 ⚠ 6/6  │  │ 颐和园   ✓ 7/10  │                │
│  │  poiId 1345      │  │  poiId 1350      │  │  poiId 1355      │                │
│  │                  │  │                  │  │                  │                │
│  │  6 cells         │  │  6 cells         │  │  10 cells        │                │
│  │  6 非自营         │  │  6 非自营         │  │  3 非自营  7 自营 │                │
│  │  最新告警 14:00   │  │  最新告警 14:00   │  │  最新告警 13:30  │                │
│  │                  │  │                  │  │                  │                │
│  │  [查看 SKU →]    │  │  [查看 SKU →]    │  │  [查看 SKU →]    │                │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘                │
│                                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐                │
│  │ 北海公园 ✓ 5/5   │  │ 景山公园 ✓ 6/6   │  │ 雍和宫  ✓ 6/6    │                │
│  │  ...             │  │  ...             │  │  ...             │                │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘                │
│                                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐                                     │
│  │ 恭王府   ⚠ 8/8   │  │ 藏文化博物院 ✓5/5│                                     │
│  │  ...             │  │  ...             │                                     │
│  └──────────────────┘  └──────────────────┘                                     │
│                                                                                  │
│                                                              [设置] [告警历史]   │  ← 浮动按钮
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- 卡片背景 `--panel`，圆角 `--radius-sm`（2px），描边 `1px solid var(--rule)`
- POI 标题 `IBM Plex Sans` 15px medium + 字间距 0
- POI 标题旁的徽章：`⚠ 6/6` 或 `✓ 5/5`——mono 11px，amber/phosphor，背景透明
- 卡片底色按"是否有非自营"切换：6/6 → 卡片左边线 2px solid amber；5/5 → 左边线 2px solid phosphor

### 2.4.2 POI 详情（`/poi/{poi_id}`）—— 按商品类型分组

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 飞猪哨兵  ● LIVE  14:23:07  上次扫描: 14:00 (23 min ago)  POI 8/8  SKU 60  ⚠ 16   │
├──────────────────────────────────────────────────────────────────────────────────┤
│ ← 返回 dashboard                                                                    │
│                                                                                     │
│ 圆明园                                                          上次扫描 14:00      │
│ poiId 1345 · 6 cells · 6 非自营 · 0 自营                                              │
│                                                                                     │
│  筛选: [全部▼] [只看非自营] [只看关注] [只看新出现]      排序: [按热度▼]            │
│ ════════════════════════════════════════════════════════════════════════════════    │
│                                                                                     │
│ ── 景点门票 · 3 cells ────────────────────────────────────────────────────────      │
│ ┃ SKU                      ┃ 卖家                ┃ 价格 ┃ 销量 ┃                  │
│ ┃──────────────────────────┃─────────────────────┃──────┃──────┃                  │
│ ┃ 成人票                   ┃ ★ 北京旭冉假期 [SELF]┃ ¥11  ┃ 1k+ ┃ ▸                │
│ ┃ item 994832029673 · 5976452104959                   ┃      ┃      ┃                  │
│ ┃──────────────────────────┃─────────────────────┃──────┃──────┃                  │
│ ┃ ...                     │  │                    │  │   │  │   │                    │
│                                                                                     │
│ ── 门票套餐 · 2 cells ────────────────────────────────────────────────────────      │
│ ┃ 大门票+西洋楼遗址+沙盘…  ┃ 宫足迹旅行社旗舰店  ┃ ¥31  ┃ 1k+ ┃ ▸                  │
│ ┃ item 1065739764221 · 6276363111198                  ┃      ┃      ┃                  │
│ ┃──────────────────────────┃─────────────────────┃──────┃──────┃                  │
│ ┃ 大门票+电子导览           ┃ 宫足迹旅行社旗舰店  ┃ ¥18  ┃ 400+┃ ▸                  │
│ ┃ item 1063764674916 · 6126605864742                  ┃      ┃      ┃                  │
│                                                                                     │
│ ── 园内项目 · 1 cell ──────────────────────────────────────────────────────────      │
│ ┃ 圆明园(电子导览)+清华大学…  ┃ 南宁哪都通旅游专营店 ┃ ¥10  ┃ 4  ┃ ▸                  │
│ ┃ item 771165079110 · 5452191778939                   ┃      ┃      ┃                  │
│                                                                                     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- **三个分组块**——每块顶部一个 24px 高的「副标题条」：
  - 左边 4×4 方块点 + uppercase tracked 11px `--text-faint` 文字「景点门票 · 3 cells」
  - 块下用 hairline rule 分隔
- **筛选栏**——POI 顶部一行：`<select>` 三个 + sort；不弹层，原生控件
- 表头 uppercase tracked：`SKU / 卖家 / 价格 / 销量`——11px，灰 `--text-faint`，间距宽
- 每行高 48px，第一列（SKU）下方附 `itemId · skuId` 11px `--text-dim`——两行信息密度
- 价格列右对齐 mono，颜色 `--text`
- **卖家列三档颜色编码**（最关键的视觉规则）：

| 角色 | 标识 | 左色条 | seller 列 |
|---|---|---|---|
| **自营 (SELF)** | `★ ... [SELF]` | 2px `--phosphor` | `--phosphor` 文字 + `[SELF]` 标签 |
| **关注 (watched)** | `★ ...` | 2px `--cyan` | `--cyan` 文字 + `★` 前缀 |
| **其他非自营** | 无 | 无（默认 row） | `--text` 文字，sellerId 兜底 |
| **新出现 (24h 内)** | ⚠ NEW | 无 + amber 横线 | 同行附 11px `--amber` 「NEW」chip，24h 后消失 |

> 关键决策：**amber 不作行色**（避免和告警/状态变更混淆），只用于「这个 cell 是 24h 内首次出现的」事件 chip。
- 价格列右对齐 mono，颜色 `--text`，**非自营行加 2px 左边线 amber**
- 「▸」展开按钮在最后一列——点击展开 inline 详情（不弹窗）：
  - **展开后**插入 `<tr>`，跨 6 列，背景 `--panel-up` 显示：
    - 左：店铺跳转链接（磷光绿）
    - 中：服务人数、icon 缩略
    - 右：历史价格（最近 10 轮 sparkline）+ 告警次数

### 2.4.3 SKU 详情（`/sku/{item_id}/{sku_id}`）

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  ← 返回 圆明园                                                                    │
│                                                                                   │
│ 大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解                                       │
│ itemId 1065739764221  ·  skuId 6276363111198  ·  poiId 1345 (圆明园)               │
│ ════════════════════════════════════════════════════════════════════════════════  │
│                                                                                   │
│ 卖家                           店铺信息                                            │
│ ─────────────────────────       ─────────────────────────                         │
│ 宫足迹旅行社旗舰店               🔗 shop409282745.m.taobao.com                      │
│ sellerId 2215602156137           服务人数 17w+                                      │
│ □ 非自营 (SELF = 2217592322543)                                                   │
│                                                                                   │
│ 价格 (¥)                                                                        │
│ ─────────────────────────                                                            │
│ 当前  ¥58     起                                                                  │
│ 10 轮 ago  ¥58     平                                                              │
│ 20 轮 ago  ¥59     ↓ -1.7%                                                        │
│ ════════════════════════════════════════════════════════════════════════════════  │
│                                                                                   │
│ 原始 booktips 响应 (折叠)                                                          │
│ [▶ 展开 JSON]                                                                      │
│                                                                                   │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- 标题区：上半部分，`IBM Plex Sans` 20px medium
- meta 行：mono 12px `--text-dim`
- 三栏 meta 信息块：左对齐标签（uppercase tracked 11px `--text-faint`）+ 下方 mono 13px 内容
- 原始 JSON：折叠 `<details>` 元素，展开后 monospace 13px，`--text-dim` 配色，max-height 600px overflow scroll

### 2.4.4 告警历史（`/alerts`）

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 告警历史                                                                          │
│                                                                                   │
│ 时间范围: [今天 ▼]    类型: [全部 ▼]    POI: [全部 ▼]    [刷新]                      │
│ ════════════════════════════════════════════════════════════════════════════════  │
│                                                                                   │
│ 时间        类型              POI    SKU / 卖家              状态                   │
│ ──────────  ────────────────  ─────  ─────────────────────  ───────                │
│ 14:00:00    非自营 SKU        圆明园  大门票+西洋楼…         │  ✓ 已推送             │
│             详情 →            宫足迹                                                            │
│ ──────────  ────────────────  ─────  ─────────────────────  ───────                │
│ 14:00:00    非自营 SKU        圆明园  圆明园+西郊机场…      │  ✓ 已推送             │
│             详情 →            成都链上                                                          │
│ ──────────  ────────────────  ─────  ─────────────────────  ───────                │
│ 13:30:00    新卖家首现        颐和园  sid 2217xxx            │  ✗ 推送失败 (retry 3) │
│             详情 →            ?                                                                 │
│                                                                                   │
│                                          [上一页]  第 1 / 8 页  [下一页]            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- 表格无背景边框，仅 1px `--rule` 横线分隔行
- 状态列：`✓ 已推送`（phosphor）/ `✗ 推送失败`（amber）/ `⏳ 重试中`（`--text-dim`）
- 过滤栏：原生 `<select>` + 按钮组，水平排开

### 2.4.5 设置（`/settings`）

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 设置                                                                              │
│ ════════════════════════════════════════════════════════════════════════════════  │
│                                                                                   │
│ Webhook 通知                                                                       │
│ ─────────────────────────                                                          │
│ 推送 URL                                                                          │
│ ┌────────────────────────────────────────────────────────────────────────────┐    │
│ │ https://oapi.dingtalk.com/robot/send?access_token=xxx                       │    │
│ └────────────────────────────────────────────────────────────────────────────┘    │
│ ☑ 新非自营 SKU 出现         ☑ 价格异动 ±20%                                       │
│ ☐ 新 sellerId 首现          ☑ 自营 SKU 突然消失                                    │
│ ☐ shelf 接口异常                                                                      │
│                                                                                   │
│ [测试推送]                                          上次成功: 14:00 (今日)          │
│                                                                                   │
│ ─────────────────────────────────────────────────────────                        │
│ 监控 POI                                                                          │
│ ─────────────────────────                                                          │
│ ☑ 圆明园    poiId 1345   30 min        ☑ 天坛公园    poiId 1350   30 min          │
│ ☑ 颐和园    poiId 1355   30 min        ☑ 北海公园    poiId 1338   30 min          │
│ ☑ 景山公园  poiId 1341   30 min        ☑ 雍和宫      poiId 1331   30 min          │
│ ☑ 恭王府    poiId 1544   30 min        ☑ 藏文化博物院 poiId 12726  30 min          │
│                                                                                   │
│ ─────────────────────────────────────────────────────────                        │
│ 登录                                                                              │
│ ─────────────────────────                                                          │
│ 修改密码:  [旧密码] [新密码] [新密码重复]  [更新]                                   │
│                                                                                   │
│ [保存所有设置]                                                                     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- 三段，每段 24px padding-bottom
- 段标题：mono 11px uppercase tracked，`--text-faint`——典型控制台 section header
- 表单元素：所有 input/select 用 `--radius-md` 4px 圆角，背景 `--panel`，描边 `1px solid var(--rule)`，focus 时变 `--phosphor`
- 「测试推送」按钮：右侧 mono 文字显示上次成功时间

### 2.4.6 卖家管理（`/sellers`）—— 列表视图

> 「关注的供应商」是用户主动打标的。这一页是卖家画像的"中央管理台"：浏览所有出现过 / 关注中的 seller，逐条编辑 display_name / is_watched / notes。

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ 飞猪哨兵  ● LIVE  14:23:07  上次扫描: 14:00 (23 min ago)  POI 8/8  SKU 60  ⚠ 16   │
├──────────────────────────────────────────────────────────────────────────────────┤
│ ← 返回 dashboard                                                                    │
│                                                                                     │
│ 卖家管理                                              关注 4 / 共 38 个 seller      │
│ ════════════════════════════════════════════════════════════════════════════════    │
│                                                                                     │
│  筛选: [全部 ▼] [只看关注] [只看未识别] [未填 display_name ▼]   搜索: [____________] │
│                                                                                     │
│ ┃ sellerId        ┃ 显示名 / 来源                 ┃ 关注 ┃ 货架数 ┃ 首次   ┃ 操作    │
│ ┃─────────────────┃───────────────────────────────┃──────┃────────┃────────┃─────────┃
│ ┃ 2217592322543   ┃ 北京旭冉假期旅游专营店        ┃ [SELF]┃ 14    ┃ 8/22   ┃ [查看]   ┃
│ ┃                  ┃ (booktips · 服务 8000+)        ┃      ┃        ┃        ┃          ┃
│ ┃─────────────────┃───────────────────────────────┃──────┃────────┃────────┃─────────┃
│ ┃ 2215602156137   ┃ 宫足迹旅行社旗舰店            ┃  ★   ┃ 9     ┃ 8/22   ┃ [编辑]   ┃
│ ┃                  ┃ (booktips · 17w+)              ┃      ┃        ┃        ┃          ┃
│ ┃─────────────────┃───────────────────────────────┃──────┃────────┃────────┃─────────┃
│ ┃ 2217xxxxxxxx    ┃ 221759…(未识别)              ┃  ☆   ┃ 1     ┃ 今日   ┃ [编辑]   ┃
│ ┃                  ┃ booktips 未拉到名                ┃      ┃        ┃        ┃          ┃
│ ┃─────────────────┃───────────────────────────────┃──────┃────────┃────────┃─────────┃
│ ┃ 2856437246      ┃ 飞猪景区乐园旗舰店            ┃      ┃ 8     ┃ 8/22   ┃ [编辑]   ┃
│ ┃                  ┃ (booktips · 4294w+)             ┃      ┃        ┃        ┃          ┃
│                                                                                     │
│                                          [上一页]  第 1 / 4 页  [下一页]            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- 整张表用 mono（sellerId 列严格对齐；"2217xxxxxxxx" 前缀对齐关键）
- 「显示名」列双行：第一行 = 当前显示名（highlighted）；第二行 = 来源（`booktips` / `manual` / `未识别`）+ 服务人数
- 「关注」列：自营 → `[SELF]` 标签（phosphor）；关注 → `★` + cyan；未关注 → `☆` + `--text-faint`（点击切换）
- 「货架数」= 该 seller 当前在 cells_snapshot 中出现的 cell 数
- 「首次」= `sellers.first_seen_at` 截断到日（"8/22" / "今日"）
- 行 hover 32ms 提亮（**注意：这一页无 row 高亮色条**——色条只用于 SKU 行）

### 2.4.7 卖家详情 / 编辑（`/sellers/{seller_id}`）

> 单条 seller 的全景视图 + 编辑表单。点列表中的 [编辑] / [查看] 进来。

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ← 返回卖家列表                                                                      │
│                                                                                     │
│ 宫足迹旅行社旗舰店                                          ★ 关注中 · priority 2   │
│ sellerId 2215602156137                                                            │
│ ════════════════════════════════════════════════════════════════════════════════    │
│                                                                                     │
│ 概况                               来源                                              │
│ ──────────────────────────────     ──────────────────────────                        │
│ shopUrl  https://shop409282745.m.taobao.com  来源:  booktips 2026-08-23 14:00       │
│ icon    [icon 32×32] 服务 17w+                                                       │
│ 首次   2026-08-22          最近 2026-08-23 14:00                                    │
│ 货架 9 个，跨 5 个 POI                                                              │
│                                                                                     │
│ 涵盖的 POI                                                                          │
│ ──────────────────────────────                                                       │
│ 圆明园 · 雍和宫 · 景山公园 · 天坛公园 · 北京动物园                                  │
│                                                                                     │
│ 最近 30 轮的货架列表                                                                │
│ ──────────────────────────────                                                       │
│ ┃ POI    ┃ SKU         ┃ 价格 ┃ 销量 ┃ 状态                                        │
│ ┃───────┃─────────────┃──────┃──────┃─────────                                      │
│ ┃ 圆明园 ┃ 大门票+西洋… ┃ ¥31  ┃ 1k+ ┃ ●已售                                        ┃
│ ┃ 圆明园 ┃ 大门票+电子… ┃ ¥18  ┃ 400+┃ ●已售                                        ┃
│ ┃ ...                                                                                │
│                                                                                     │
│ ────────────────────────────────────────────────────                                │
│ 编辑（保存）                                                                       │
│ ──────────────────────────────                                                       │
│ display_name  [宫足迹旅行社旗舰店____________________]                               │
│ ☑ 关注                                                                priority [2▼] │
│ notes         [本 POI 主对手，9 个 SKU 分布均匀_______________]                     │
│ tags          [competitor, vip_____________]  (逗号分隔)                            │
│                                                                                     │
│ [保存修改]                                          最近一次编辑  2026-08-23 14:00   │
└──────────────────────────────────────────────────────────────────────────────────┘
```

#### 视觉规则

- **概况 / 来源** 双栏布局：左是结构性事实（店铺 / icon / 服务人数），右是数据来源（"booktips 2026-08-23 14:00" / "manual 2026-08-22"）
- 「涵盖的 POI」= chips 列表：每个 chip 12px mono、边框 hairline、点击跳 `/poi/{poi_id}?focus=seller:{seller_id}`
- 「最近 30 轮的货架列表」= 内嵌缩小版 SKU 主表，无展开交互
- 「编辑（保存）」段独立一个 panel（背景 `--panel-up`），表单元素同设置页风格
- **乐观保存**：点 [保存修改] → 立即 POST → 成功后页面顶部出"已保存"toast（2s 自动消失），无需等待
- 「最近一次编辑」显示 `seller_enrichment.updated_at`——若用户从未编辑过，显示"—"

---

## 2.5 顶部状态条 详解

```html
<header class="topbar">
  <div class="topbar__brand">飞猪哨兵</div>
  <div class="topbar__status">
    <span class="dot dot--live"></span>
    <span class="label">LIVE</span>
  </div>
  <div class="topbar__clock" id="clock">14:23:07</div>
  <div class="topbar__meta">
    <span class="meta-item">上次扫描 14:00 <em>(23 min ago)</em></span>
  </div>
  <div class="topbar__counts">
    <span class="count">POI <b>8</b>/8</span>
    <span class="count">SKU <b>60</b></span>
    <span class="count count--alert">⚠ <b>16</b></span>
  </div>
</header>
```

#### CSS

```css
.topbar {
  height: 56px;
  background: var(--panel);
  border-bottom: 1px solid var(--rule-bold);
  display: flex; align-items: center;
  padding: 0 var(--sp-5);
  gap: var(--sp-5);
  font-family: var(--font-body);
  font-size: var(--fz-md);
  color: var(--text);
  position: sticky; top: 0; z-index: 100;
}
.topbar__brand { font-family: var(--font-display); font-weight: 500; letter-spacing: 0.04em; }
.topbar__status { display: flex; align-items: center; gap: 6px; }
.topbar__status .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--phosphor);
  box-shadow: 0 0 0 0 rgba(79,208,184,0.6);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%,100% { box-shadow: 0 0 0 0 rgba(79,208,184,0.5); }
  50%     { box-shadow: 0 0 0 6px rgba(79,208,184,0); }
}
.topbar__clock { font-family: var(--font-mono); font-size: var(--fz-md); color: var(--phosphor); }
.topbar__meta em { color: var(--text-dim); font-style: normal; }
.topbar__counts { margin-left: auto; display: flex; gap: var(--sp-4); }
.topbar__counts .count { font-family: var(--font-mono); font-size: var(--fz-sm); color: var(--text-dim); }
.topbar__counts .count b { color: var(--text); font-weight: 500; }
.topbar__counts .count--alert b { color: var(--amber); }
```

---

## 2.6 SKU 主表（核心组件）

```html
<table class="sku-table">
  <thead>
    <tr>
      <th class="col-sku">SKU</th>
      <th class="col-type">类型</th>
      <th class="col-seller">卖家</th>
      <th class="col-price">价格</th>
      <th class="col-sold">销量</th>
      <th class="col-action"></th>
    </tr>
  </thead>
  <tbody>
    <!-- 自营行：左边线 phosphor -->
    <tr class="sku-row sku-row--self" hx-get="/sku/.../detail-partial" hx-target="next tr" hx-swap="outerHTML">
      <td class="col-sku">
        <div class="sku-name">成人票</div>
        <div class="sku-meta">item 1075061329400 · sku 6125781693905</div>
      </td>
      <td class="col-type"><span class="tag-badge">景点门票</span></td>
      <td class="col-seller">
        <div class="seller-name">北京旭冉假期旅游专营店</div>
        <div class="seller-meta">sid 2217592322543 · 自营</div>
      </td>
      <td class="col-price">¥<b>59</b></td>
      <td class="col-sold">800+</td>
      <td class="col-action"><span class="arrow">▸</span></td>
    </tr>
    <!-- 非自营行：左边线 amber -->
    <tr class="sku-row sku-row--alert" hx-get="/sku/.../detail-partial" hx-target="next tr" hx-swap="outerHTML">
      <td class="col-sku">
        <div class="sku-name">大门票+西洋楼遗址+沙盘全景模型展+电子语音讲解</div>
        <div class="sku-meta">item 1065739764221 · sku 6276363111198</div>
      </td>
      <td class="col-type"><span class="tag-badge">门票套餐</span></td>
      <td class="col-seller">
        <div class="seller-name">宫足迹旅行社旗舰店</div>
        <div class="seller-meta">sid 2215602156137 · 服务 17w+</div>
      </td>
      <td class="col-price">¥<b>31</b><sup>.36</sup></td>
      <td class="col-sold">1000+</td>
      <td class="col-action"><span class="arrow">▸</span></td>
    </tr>
    <!-- 展开行 -->
    <tr class="sku-detail" style="display:none">
      <td colspan="6">
        <div class="sku-detail__inner">
          <div class="sku-detail__left">
            <a href="https://shop409282745.m.taobao.com" target="_blank">🔗 访问店铺</a>
            <a href="https://feizhu.19880913.xyz/raw/sku/1065739764221">📋 booktips 原文</a>
          </div>
          <div class="sku-detail__mid">
            <div class="meta-label">服务人数</div><div class="meta-val">17w+</div>
            <div class="meta-label">最近告警</div><div class="meta-val">3 次 / 7 天</div>
          </div>
          <div class="sku-detail__right">
            <div class="meta-label">价格历史 (10 轮)</div>
            <!-- sparkline SVG 内联 -->
            <svg viewBox="0 0 120 32" class="sparkline">
              <polyline points="0,16 12,16 24,16 36,18 48,18 60,16 72,14 84,14 96,12 108,12 120,10" stroke="var(--amber)" fill="none" stroke-width="1.5"/>
            </svg>
          </div>
        </div>
      </td>
    </tr>
  </tbody>
</table>
```

#### 关键 CSS 片段

```css
.sku-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: var(--fz-sm); }
.sku-table thead th {
  text-align: left;
  font-family: var(--font-body);
  font-weight: 500;
  font-size: var(--fz-xs);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-faint);
  padding: var(--sp-3) var(--sp-4);
  border-bottom: 1px solid var(--rule-bold);
}
.sku-table thead .col-price { text-align: right; }
.sku-table thead .col-sold  { text-align: right; }
.sku-row { border-bottom: 1px solid var(--rule); cursor: pointer; transition: background 80ms ease-out; }
.sku-row:hover { background: var(--panel-up); }
.sku-row td { padding: var(--sp-3) var(--sp-4); vertical-align: middle; }

/* ── 三档角色色条 ─────────────────────────────────── */
.sku-row--self    { box-shadow: inset 2px 0 0 var(--phosphor); }   /* 自营 */
.sku-row--watched { box-shadow: inset 2px 0 0 var(--cyan); }       /* 关注 */
.sku-row--alert   { box-shadow: inset 2px 0 0 var(--amber); }      /* 新出现 (24h 内事件 chip) */

.col-sku .sku-name { color: var(--text); font-weight: 500; }
.col-sku .sku-meta { color: var(--text-dim); font-size: var(--fz-xs); margin-top: 2px; }

/* seller 列按角色着色 */
.col-seller .seller-name { color: var(--text); }
.col-seller .seller-meta { color: var(--text-dim); font-size: var(--fz-xs); margin-top: 2px; }
.sku-row--self    .seller-name { color: var(--phosphor); }
.sku-row--watched .seller-name { color: var(--cyan); }
.sku-row--self .badge-self    { background: transparent; color: var(--phosphor); border: 1px solid var(--phosphor); padding: 0 4px; border-radius: 2px; font-size: 10px; margin-left: 6px; }
.sku-row--watched .badge-star::before { content: "★ "; color: var(--cyan); font-size: 12px; }

/* 新出现 chip — 24h 衰减 */
.sku-row--alert .badge-new {
    display: inline-block;
    background: transparent; color: var(--amber); border: 1px solid var(--amber);
    padding: 0 4px; border-radius: 2px; font-size: 10px; margin-left: 6px;
    animation: badge-pulse 2s ease-in-out infinite;
}
@keyframes badge-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }

.col-price { text-align: right; color: var(--text); }
.col-price sup { font-size: 9px; color: var(--text-dim); }
.col-sold { text-align: right; color: var(--text-dim); }
.col-action .arrow { color: var(--text-faint); transition: transform 80ms; }
.sku-row.is-open .col-action .arrow { transform: rotate(90deg); color: var(--phosphor); }

/* hover 时角色色条更亮 */
.sku-row--self:hover    { box-shadow: inset 2px 0 0 var(--phosphor), inset 0 0 0 999px rgba(79,208,184,0.04); }
.sku-row--watched:hover { box-shadow: inset 2px 0 0 var(--cyan),     inset 0 0 0 999px rgba(94,174,255,0.04); }
```

---

## 2.7 登录页（`/login`）

> 唯一一个"消费者级"的页面——但仍保持同一套色板和字体，不破调性。

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│                                                          │
│                                                          │
│                   飞猪哨兵 · FLIGGY SENTINEL               │
│                                                          │
│              ┌──────────────────────────────┐             │
│              │  password                     │             │
│              └──────────────────────────────┘             │
│                                                          │
│              ┌──────────────────────────────┐             │
│              │         登 录                 │             │
│              └──────────────────────────────┘             │
│                                                          │
│              [错误提示区域]                                │
│                                                          │
│                                                          │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

- 居中 360×200 卡片，背景 `--panel`，描边 `--rule`
- 标题 `IBM Plex Sans Condensed` 24px 字间距 0.1em，正下方 mono 11px tracked+0.2em `FLIGGY SENTINEL`
- input 高度 40px，背景 `--ink`，描边 `--rule`，focus 变 `--phosphor`
- 按钮：phosphor 背景 + ink 文字，hover 提亮
- 错误提示：`--amber` 文字 + 同色左边线 2px
- 失败 5 次 → 锁 10 分钟（IP 维度，session 计数）

---

## 2.8 HTMX / Alpine.js 用法约定

#### HTMX（局部刷新）

```html
<!-- 主表格 30s 自动刷新 -->
<table hx-get="/api/cells?poi=1345"
       hx-trigger="every 30s"
       hx-swap="outerHTML"
       hx-indicator=".refresh-indicator">
  ...
</table>

<!-- 展开行（点击 row 拉部分模板插入下一行） -->
<tr hx-get="/sku/{item}/{sku}/detail-partial"
    hx-target="next tr"
    hx-swap="outerHTML"
    hx-trigger="click">
```

#### Alpine.js（小交互）

```html
<!-- POI 启停 checkbox（点击立即写库 + 乐观 UI） -->
<div x-data="{ enabled: true }">
  <input type="checkbox" x-model="enabled"
         @change="$fetch('/api/config/poi/1345', { method:'POST', body: JSON.stringify({enabled}) })">
</div>

<!-- 顶部告警 tape 滚动 -->
<div x-data="tape()" x-init="start()">
  <template x-for="alert in alerts" :key="alert.id">
    <div class="tape__item" x-text="alert.text"></div>
  </template>
</div>
```

#### 不做的事
- ❌ 不用 React/Vue/SPA——所有页面服务端渲染
- ❌ 不用 Tailwind——手写 CSS 反而精简
- ❌ 不用大型图标库——inline SVG 即可
- ❌ 不做主题切换——单色（暗）已经定

---

## 2.9 响应式（桌面优先，向下兼容）

| 断点 | 行为 |
|---|---|
| `≥ 1280px` | 三列 POI 卡片网格（默认） |
| `960–1279px` | 两列 POI 卡片网格 |
| `< 960px` | 单列；顶部状态条压缩；表格横向滚动（`overflow-x:auto`，包在 `.table-wrap`） |
| `< 640px` | 折叠左侧 POI 列表为顶部下拉；告警 tape 隐藏 |

> 单用户桌面端工具，移动端只是「能看」。不强求所有交互完美。

---

## 2.10 自检清单（实施后必须过）

- [ ] 顶栏时钟每秒走、肉眼可分辨
- [ ] 8 个 POI 卡片在 1280px 视口正好 3 列、整屏可见
- [ ] SKU 表头 uppercase tracked 0.08em，间距正确
- [ ] 所有 mono 字段字符等宽对齐，列严格对齐
- [ ] 非自营行左边线 amber、自营 phosphor、关注 cyan——三档肉眼可分辨
- [ ] 「展开行」inline 插入，不刷新页面，不抖动
- [ ] 「上次扫描」动态更新到「刚刚」/「X min ago」
- [ ] 设置页改 webhook URL 后 1 秒内可见「测试推送」按钮启用
- [ ] POI 详情按 cell_type 分组；筛选栏工作（只看非自营 / 只看关注 / 只看新出现）
- [ ] 「关注的供应商」`★` + cyan 在 SKU 行 / 卖家列表 / 详情页三处一致
- [ ] `/sellers` 列表「关注」列点击 `☆ → ★` 立即切换（乐观 UI）
- [ ] `/sellers/{id}` 编辑表单保存后 1 秒内顶部出「已保存」toast
- [ ] 未识别的 sellerId 在所有出现位置显示截断 ID + 「点击补全」提示
- [ ] 登录失败 5 次 → 锁 10 分钟（刷新页面仍是锁）
- [ ] `prefers-reduced-motion` 时所有动画停
- [ ] 1280px / 1024px / 768px 三个视口截图都过
- [ ] Lighthouse Accessibility ≥ 95
- [ ] 首屏 < 200KB JS + 50KB CSS（gzip 前）

---

## 2.11 接下来

实施阶段：
1. 写 `web/server.py`（FastAPI + lifespan + 路由）
2. 写 `web/templates/base.html` + 子模板（继承 + block）
3. 写 `web/static/css/main.css`（先 tokens、再 layout、再组件）
4. 写 `web/static/js/app.js`（HTMX + Alpine 初始化）
5. 用 sqlite + 假数据本地起 uvicorn，截图验证
6. 部署到 VPS