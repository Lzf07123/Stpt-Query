# edu-query-app · 前端实现速览（MASTER）

> 版本：V1.0 ｜ 日期：2026-08-25 ｜ 状态：首次设计落地快照
> 代码事实优先：令牌源 `frontend/src/index.css`；运行时产物 `frontend/static/style.css`；
> 品牌单点 `frontend/static/brand.js`。本文档与代码冲突时以代码为准并回写本文档。

## 1. 文件与单一事实来源

| 层级 | 文件 | 职责 |
| --- | --- | --- |
| 品牌意图 | `design-system/edu-query-app/BRAND.md` | 定位、原则、槽位 1–22、验收基线 |
| 实现速览 | 本文件 | 令牌/组件/页面模式落地快照 |
| 令牌源 | `frontend/src/index.css` | Tailwind CSS 4 源（模板实例化 + 应用样式） |
| 运行时样式 | `frontend/static/style.css` | CLI 编译产物（自托管，零远程资源） |
| 品牌资产 | `frontend/static/brand.js` + `static/image/` | 名称/slogan/页脚/备案占位唯一出处 |
| 构建脚本 | `frontend/package.json` | `npm run build` 重新生成 style.css |

## 2. 令牌快照（`--eduquery-*`）

- 浅色：bg `#F6FBF9` / surface `#FFFFFF` / surface-2 `#EEF6F3` / fg `#35423F` /
  muted `#64736C` / border `#E1ECE8`；primary `#25786D` / hover `#1F6359` /
  soft `#D9F4EE` / fg `#FFFFFF`；secondary `#2F678F` / soft `#DFF1FA`。
- 语义色（浅，AA 调校）：success `#2A7C52`、warning `#9A5C05`、
  destructive `#C43737`（soft `#E3F6E9` / `#FDF3D8` / `#FDEEEE`）。
- 深色：D1 雾灰 `#3A3F45` / `#434950` / `#4B5259`；fg `#F0F2F4`、muted `#B8C0C7`、
  border `#545C64`；primary `#7FD4C6`、hover `#A5E4D9`、soft `rgba(127,212,198,.16)`、
  fg `#17332E`；语义深色 `#86D6AC` / `#EAD48E` / `#E8A49A`。
- 深色带文字软底：`*-soft-solid` 实色粉彩 + `*-soft-fg` 深字
  （primary `#D9F4EE`/`#17332E`，success `#E3F6E9`/`#14532D`，warning `#FDF3D8`/`#78350F`，
  destructive `#FDEEEE`/`#7F1D1D`）。
- 按钮：浅 bg `rgba(47,127,116,.10)` / hover `.17` / 描边 `.26`；
  深 bg `rgba(127,212,198,.13)` / hover `.21` / 描边 `.30`；扫光 `--eduquery-btn-sweep`。
- 液态玻璃：`--eduquery-glass-*` 八件套 + `blur 18px` / `saturate 150%`（明暗两套）。
- 阴影三档（水绿 tint，透明度总和 < 0.1）、缓动 `--ease-out` / `--ease-spring`、
  时长 `--motion-fast/base/slow = 150/250/350ms`；前缀对齐别名
  `--eduquery-motion-*` / `--eduquery-ease-*` 供视觉刷新模块引用。
- 明暗切换：`html.dark`；主题存储键 `eduquery-theme`。

## 3. 组件与页面模式

| 模式 | 落地类 | 说明 |
| --- | --- | --- |
| 页面外壳 | `.page-shell`（外套）> `.wrap`（内层） | 底部 96px 留白、顶部 32px；`.wrap` `overflow-x:clip` 防辉光外溢 |
| 居中标题 | `.page-title` / `.section-title` | 48×3px 主色装饰线 |
| 认证表单卡 | `.card.card-signature.card-halo` | 签名描边 9s + 呼吸辉光 4.5s；内边距 24px（≥640px 为 24×28） |
| 输入/下拉 | `.input`、`.dropdown` / `.dropdown-trigger` / `.dropdown-menu` / `.dropdown-item` | 触发器 48px、选项 44px 触达；菜单 `max-height:min(320px,40vh)` 滚动、不超出视口；焦点 ring 主色；键盘契约由既有 JS 提供 |
| 分段选择 | `.segmented` / `.seg-btn` | 查询项目（课表/成绩），active 半透明主色底 |
| 复选框 | `input[type=checkbox]` + `.field-check` | 18px、accent-color 主色 |
| 按钮 | `.btn` / `.btn-primary` / `.btn-ghost` / `.btn-sm` | 半透明单色 + 扫光 + 涟漪；查询/清空整行 7:3，控件统一 48px |
| 结果卡 | `.project-card`（液态玻璃） | hover 上浮 3px、顶部主色细线展开 |
| 结果表格 | `.table-shell`（规范表格组件） | 表头 surface-2、行 hover `bg-surface-2/60`；移动端横向滚动（表格 `min-width:480px`） |
| 加载骨架 | `.search-skeleton` 结构（`#skeleton` 内） | shimmer 1.35s |
| 空状态 | `.empty-state` / `.empty-state-art` / `.empty-state-text` | SVG 小图 + 引导文案 |
| 提示/状态 | `.notice` 变体、`.status`（ok/error）、`.status-dot` | 语义色只表状态 |
| Toast | `.toast` / `.toast-success` / `.toast-error` / `.toast-info` | 进度条 + 进入/离开动画 |
| 服务状态 | `.service-status` / `.status-seg` / `.status-bar` | 最近 100 次查询可用性 |
| 页脚 | `.site-footer` / `.site-footer-inner` / `.filing-icon-placeholder` | 56px 单行、muted 链接、brand.js 驱动 |
| 返回顶部 | `.back-to-top` | 纯锚点 44×44 |
| 氛围层 | `.tech-ambience--soft` + `.tech-grid` | 工作台 soft；移动端停用/减量 |

## 4. 页面模式：单页查询工具

- 顶部居中标题 + 主题切换；主区默认居中单列（查询表单卡 680px），有结果后
  ≥960px 切「340px 表单 + 自适应结果玻璃卡」双栏（结果卡 sticky）；窄屏单栏；
  服务状态条居中 680px；右下角返回顶部；`site-footer` 贴底。
- 结果渲染：服务端 Markdown → 本地零依赖解析（表格/引用/列表/代码），
  成绩列三档语义着色（高/中/低）+ 课程类别徽章；表格走规范 `.table-shell`
  （含行 hover 效果），移动端横向滚动。
- 查询流程状态：loading（spinner + 骨架）→ success（toast + 结果）/ failure（notice）。
- 响应契约：优先识别后端统一结构 `success/kind/meta`（错误 kind 渲染分类报告且不写历史，
  `grades_empty` 走空状态），保留 Dify 风格 `_meta`/正则兜底；结果行数按 Markdown 表体行
  计数（避免与 200ms 淡入计时器竞态）。
- PDF：`pdf_base64` 经 Base64 解码为 Blob 下载，结果卡同时渲染「下载 PDF 文档」按钮；
  文件名 `成绩单_<学号>_<时间戳>.pdf`，清理延后 60s 以免中断下载。
- 下拉/菜单：触发器与选项完整键盘契约（Arrow/Home/End/Enter/Space/Escape/Tab），
  `role=option` + `aria-selected` 同步，Escape 关闭后焦点回到触发器。
- 叠层：`.card-halo` 提升 z-index，防止其 isolation 叠层上下文使页脚盖住下拉菜单；
  按钮涟漪统一用模板 `.btn-ripple`。
- 文字居中：表单标签、输入值/占位符、下拉触发器与选项、提示条、复选框文案均居中；
  服务状态条未捕获格子以 `--eduquery-border` 显示占位。

## 5. 全量视觉规范验收（2026-08-25，计算样式逐值审计）

**Li-Design 视觉刷新 + 品牌方案 Pre-Delivery 清单：27/27 通过**（无头 Chrome 实测）：

- 令牌逐值：浅/深两套 `--eduquery-*` 与模板一致；液态玻璃 `blur(18px) saturate(150%)`、
  `glass-bg 0.58/0.56`、内高光 `rgba(255,255,255,.82) 1px inset`。
- 留白与标题：`.page-shell` 底 96px/首屏 32px；主标题居中；区块标题 48×3px 装饰线。
- 卡片：表单卡 16px + 签名描边；结果玻璃卡 20px、内边距 24×28、边框
  `rgba(225,236,232,.66)`；hover `matrix(1,0,0,1,0,-3)` + 顶部主色细线 `scaleX(0→1)`。
- 页脚：`mt-auto` 贴底、单行 56px、12px、半透明表面 + `blur(8px)`、链接 muted
  `rgb(100,115,108)`；返回顶部 44×44、桌面 20/24、移动 14/16、圆角 14px。
- 触控与控件：输入/下拉/分段/按钮 44px；复选框 18px + 主色 accent；全部可点击
  `cursor:pointer`；聚焦主色边框 + 2px ring。
- 深色 AA：带文字软底回退 soft-solid/soft-fg，实测 CR：required 11.70、practice 8.20、
  elective 6.75、seg/dropdown.active 11.70（全部 ≥ 4.5）。
- 空状态 48×24/圆角 22/图标盒 84/圆角 28；骨架 shimmer 1.35s（220%）；每 26 个
  animation 均有 @keyframes；`prefers-reduced-motion` 单帧。
- 断点：320/390/769/1440 四档 `scrollWidth == innerWidth`，无横向溢出。
- 源码：`{{PROJECT_PREFIX}}` 零残留；组件层无硬编码 hex；页面无 emoji 图标。

**规范表格组件对齐（2026-08-25 补充）**：Markdown 结果表格由项目自有 `.table-scroll`
改为规范 `.table-shell`（移除重复实现），行 hover 计算样式实测 `oklab(0.966 -0.009 0.001 / 0.6)`
（= `surface-2` 60%）；`min-width:480px` 保留横向滚动；320/390/768/769/1024/1440 六档
`scrollWidth == innerWidth` 无横向溢出；无头 Chrome 无页面 JS 异常（静态预览下
`/service-status` 404 属预期，网关反代后提供）。

**功能回归 17/17 通过**：品牌单点/主题持久化/分段联动/下拉鼠标+键盘契约/密码显隐/
学号校验/后端失败分类渲染/服务状态条/成绩表格着色与徽章/空状态/课表下载/PDF 落盘/
渲染器 XSS 防护/无 JS 报错。

验收截图（Chrome DevTools 实测基准）：

- `preview/light-home-1440.png`、`preview/dark-home-1440.png`
- `preview/light-result-1440.png`（玻璃卡 + 分类报告 + 服务状态条）
- `preview/light-home-390.png`（移动端）

## 6. 令牌校验与验收证据

- 仅槽位差校验：模板实例化后 `--eduquery-*` 令牌集合与模板逐值一致（70/70）；
  仅新增 5 个前缀对齐别名（`--eduquery-motion-*` / `--eduquery-ease-*`，
  与无前缀 `--motion-*` / `--ease-*` 同值）。
- 计算样式抽查（无头 Chrome，本地静态预览）：1440/769/390/320 四档
  `scrollWidth == innerWidth`；结果卡 `backdrop-filter: blur(18px) saturate(1.5)`、
  圆角 20px；`.back-to-top` 桌面 20/24、移动 14/16；`.theme-toggle` 44×44；
  页脚链接 muted（浅 `rgb(100,115,108)`）；移动端氛围光点 `display:none`。
- 明暗模式：浅 `html` 背景 `rgb(246,251,249)`、深 `rgb(58,63,69)`（D1 雾灰）。

## 7. 构建与验收

```bash
cd frontend
npm install            # tailwindcss + @tailwindcss/cli（仅构建期依赖）
npm run build          # src/index.css → static/style.css（--minify）
```

验收清单（BRAND.md §5）逐项核对；四档视口 375/768/1024/1440 无横向滚动；
`prefers-reduced-motion` 收敛单帧；`rg '{{PROJECT_PREFIX}}' frontend/src/index.css`
无占位符残留；`rg '#[0-9a-fA-F]{3,8}' frontend/static/index.html` 无硬编码颜色。
