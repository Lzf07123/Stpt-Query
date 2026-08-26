# edu-query-app · 项目品牌方案（BRAND）

> 版本：V1.0 ｜ 日期：2026-08-25 ｜ 状态：首次设计定稿
> 模板来源：`Li-Design` 子模块 `REUSABLE-BRAND-SCHEME.md`（V1.5.2）与
> `REUSABLE-VISUAL-REFRESH-2026-08-24.md`（V1.0）。本文件是项目内方案事实；
> 后续开发以本文件与落地代码为准，模板仓库仅作升级对照，不是运行时依赖。

## 1. 项目与定位

edu-query-app 把固定的「汕职院教务信息查询」Dify 工作流重写为三容器编排服务。
前端是唯一对外入口（Nginx 静态页 + 反代），面向师生提供成绩/课表查询、成绩分析与
PDF 导出。

| 维度 | 内容 |
| --- | --- |
| 一句话定位 | 一次登录，查清成绩与课表 |
| 品牌承诺 | 一次认证，安心查询；结果可复现、过程可追溯 |
| 人格关键词 | 可靠、克制、专业、流畅、安静 |
| 人格比喻 | 教务助手：不问多余的话，不制造惊吓，把查询结果清清楚楚交到手里 |
| 避免成为 | 营销落地页、吓人的安全警告、冷冰冰的政务系统 |

## 2. 品牌内核（继承 Li& 模板，不偏离）

### 2.1 五大设计原则（TRUST 内核）

1. **信任优先**：结果与错误都直接、可行动；登录/令牌风险提示诚实但不唬人。
2. **淡色科技感**：海玻璃主色 `#25786D`（浅）/ `#7FD4C6`（深），全淡色系、无粉色、
   无大面积重色；按钮半透明单色着色，不用渐变霓虹。
3. **以动衬静**：氛围光效极慢极淡（工作台 `soft` 档），只动 transform/opacity/
   background-position，永不阻塞交互；尊重 `prefers-reduced-motion`。
4. **单一事实来源**：颜色/间距/阴影/动效只在 `frontend/src/index.css` 令牌；
   品牌文案默认值只在 `frontend/static/brand.js`，运行时可由 `brand-env.js`（容器启动按
   环境变量生成）覆盖；提示词/分类/渲染单点维持后端既有契约。
5. **无障碍与节能**：正文对比 ≥ 4.5:1，焦点可见，触达 ≥ 44px，移动端减量。

### 2.2 视觉语法：几何暗线（符号重映射）

| 符号 | 本项目管理学映射 | 用法 |
| --- | --- | --- |
| 细直线 | 查询链路 | 背景层低频穿行 |
| Z 形折线 | 统一认证 / 免密跳转 | 品牌签名形，最多 1 个 |
| 方块 | 成绩单 / 课表票据 | 往复钟摆，稳定存在感 |
| 锁钥组合 | 统一身份认证 | 仅信任关键时刻 |
| 圆点光斑 | 会话 / 数据流 | 盘旋公转，安静存在 |

符号铁律不变：低透明度 0.04–0.25、背景层、几何、无滤镜动画、不碰文本/表格/按钮。

### 2.3 氛围动效

- 单页查询工具按「工作台/后台」浓度分层：`tech-ambience--soft`（网格 + 少量光点，
  移动端隐藏光束/光点并停用网格动画），不启用 full 极光层。
- 循环元素 `animation-iteration-count: infinite`、`pointer-events: none`；
  `prefers-reduced-motion` 单帧；移动端 <768px 元素数 ≤ 6。

### 2.4 文案语调

- 动词开头：查询、清空、重试、下载 PDF。
- 错误可行动：「凭据被拒绝：请核对教务系统账号密码，确认后重试」而非「登录失败」。
- 安全提示直接但不恐吓；数字精确（最近 N 次查询、可用性百分比）。

## 3. 项目适配层（槽位表，22 项）

| # | 槽位 | 决策值 | 理由 |
| --- | --- | --- | --- |
| 1 | 项目显示名 | 教务信息查询（repo：edu-query-app） | 与页面功能一致，沿用原站点标题 |
| 2 | 技术标识 | `eduquery` | 小写、唯一，贯穿 CSS 前缀/主题键 |
| 3 | 一句话定位 | 一次登录，查清成绩与课表 | §1 |
| 4 | 品牌承诺 | 一次认证，安心查询；结果可复现、过程可追溯 | §1 |
| 5 | 人格比喻 | 教务助手 | §1 |
| 6 | 符号隐喻 | §2.2 | 教育域重映射 |
| 7 | 主色（浅） | `#25786D` / hover `#1F6359` / soft `#D9F4EE` / fg `#FFFFFF` | 家族海玻璃 700 档，教育域亲和、克制 |
| 8 | 主色（深） | `#7FD4C6` / hover `#A5E4D9` / soft `rgba(127,212,198,.16)` / fg `#17332E` | 300 档雾面浅色 |
| 9 | 中性色（浅） | bg `#F6FBF9` / surface `#FFFFFF` / surface-2 `#EEF6F3` / fg `#35423F` / muted `#64736C` / border `#E1ECE8` | 模板 AA 调校，muted 4.77:1 |
| 10 | 中性色（深） | `#3A3F45` / `#434950` / `#4B5259` / `#F0F2F4` / `#B8C0C7` / `#545C64` | D1 雾灰中间调，不压黑 |
| 11 | 语义色 | 浅 success `#2A7C52` / warning `#9A5C05` / destructive `#C43737`；深 `#86D6AC` / `#EAD48E` / `#E8A49A`；深色带文字回退 soft-solid + soft-fg | V1.3 AA 值，全部 ≥ 4.5:1 |
| 12 | 焦点环 | 2px 主色描边 + 2px offset（浅 `#25786D`，深 `#7FD4C6`） | 全局 `:focus-visible` |
| 13 | 字体栈 | 系统栈 → PingFang SC / 微软雅黑，**不加载远程字体** | 零外部资源、教务工具可读性优先 |
| 14 | 标题字体 | 不启用独立标题字体 | 零远程资源；统一系统栈 |
| 15 | Logo / favicon | 复用 `frontend/static/image/icon.svg` | 既有资产，SVG 单格式 |
| 16 | 令牌前缀 | `eduquery`（`--eduquery-*`） | 槽位 2 |
| 17 | 主题存储键 | `eduquery-theme` | 首帧脚本与主题切换共用 |
| 18 | slogan / 备案 | slogan「汕职院教务信息查询」；ICP/公安备案上线前留空占位，不写假号；名称/slogan/description/页脚作者与 GitHub 链接可由 `BRAND_*` 环境变量覆盖 | 单点 `brand.js` 默认值 + `brand-env.js` 运行时覆盖 |
| 19 | 氛围浓度 | 工作台/后台 `soft`（网格 + 3 束扫掠光束 + 少量光点；移动端隐藏光束/光点并更减量） | 单页工具，可读性优先 |
| 20 | 浏览器品牌位 | favicon `icon.svg`、`theme-color` 明暗两套、`description`、首帧主题脚本 | `index.html` |
| 21 | 强调色板 | 模板六色相 strong/soft（ice/aqua/lilac/sage/mint/sand） | 仅图例/状态分区小面积，≤15% |
| 22 | 按钮与光效 | 半透明单色按钮（浅 10% / 深 13% + 细描边 + 扫光）；光效「可见但克制」 | 模板默认 |

## 4. 落地映射（前端为原生静态页的实例化路径）

- 本仓库前端形态为「Nginx 原生静态 HTML/CSS/JS」，与 Li&Chat 同类；按方案 §8.2
  「前端结构与默认假设不同时，按项目实际结构映射」执行：
  - `frontend/src/index.css`（模板令牌源，`@import "./app.css"` 承载查询页特有形态）→
    构建期 Tailwind CSS 4 CLI 编译 → `frontend/static/style.css`
    （运行时唯一样式，自托管，零远程资源）。
  - `frontend/src/lib/brand.ts` → `frontend/static/brand.js`（品牌单点）。
  - `frontend/index.html` → `frontend/static/index.html`。
- 技术栈不变：样式层 Tailwind CSS 4 同栈（`@theme` 语义别名 + `@apply` 组件），
  效果逐值对齐模板；运行时页面不引入任何第三方/远程资源。
- 只移植需要的组件：card/card-signature/card-halo、input、btn/btn-primary/btn-ghost、
  dropdown-menu、notice、toast、table-shell、badge、spinner、btn-ripple、site-footer、
  page-shell/section/page-title/section-title、project-card（液态玻璃）、search-skeleton
  （查询加载）、empty-state（空结果）、back-to-top、tech-ambience--soft。
- 不适用项（明确跳过，非偏离）：瀑布流/真实封面（无内容列表）、移动端二级菜单
  （单页无导航）、搜索索引骨架（无站内搜索）。查询页特有形态（表单行、分段选择、
  Markdown 结果渲染、服务状态条）在 `app.css` 以令牌实现并记录于 MASTER.md。
- 外壳采用 `.page-shell` 外套 + 内层 `.wrap` 分层，外层未分层 padding 与内层令牌
  间距互不覆盖；`.wrap { overflow-x: clip }` 裁剪卡片辉光（`.card-halo::before`
  外扩 18px），保证四档视口无横向滚动。

## 5. 验收基线（Pre-Delivery Checklist 摘要）

- 无 emoji 充当图标；全部内联 SVG；无硬编码 hex（组件层）；文案默认值由 brand.js 单点，运行时经 `BRAND_*` 环境变量覆盖（`BRAND_GITHUB=none` 隐藏页脚链接）。
- 浅色正文对比 ≥ 4.5:1；`focus-visible` 2px 主色描边；`prefers-reduced-motion` 单帧。
- 375px / 768px / 1024px / 1440px 四档响应式，无横向滚动，无内容被固定导航遮挡。
- 页脚链接 muted、hover 转前景色；可点击元素 `cursor-pointer`；动效 150–300ms。
- 每个 animation 有对应 @keyframes；移动端氛围元素 ≤ 6。

## 6. 治理

代码事实优先：`frontend/src/index.css`（源）与 `static/style.css`（编译产物）冲突时
以源文件为准并重新构建；令牌变更先改 BRAND.md 理由 → 改源 CSS → 重建 → 回写 MASTER.md。
