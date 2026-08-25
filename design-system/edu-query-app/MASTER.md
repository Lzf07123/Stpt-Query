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
| 扫掠光束 | `.tech-beam` ×3（默认 / `--violet` / `--sage`） | 斜切 16° 透明渐变带 `translateX(-150%→560%)` 扫过并长停顿，10s、错峰 0.8/4.2/7.5s；soft 降浓度；<768px 隐藏 |

## 4. 页面模式：单页查询工具

- 顶部居中标题 + 主题切换；主区默认居中单列（查询表单卡 680px），查询后隐藏表单、
  返回结果卡单列占满全屏（`min-height:min(72vh,880px)`、容器上限 1600px），
  结果卡右上角「返回查询」按钮回到表单并聚焦学号；服务状态条居中 680px；
  右下角返回顶部；`site-footer` 贴底。
- 结果渲染：服务端 Markdown → 本地零依赖解析（表格/引用/列表/代码），
  成绩列三档语义着色（高/中/低）+ 课程类别徽章；表格走规范 `.table-shell`
  （含行 hover 效果），移动端横向滚动。
- 查询流程状态：loading（spinner + 骨架）→ success（toast + 结果）/ failure（notice）；
  提交即清除上一次结果渲染定时器，缓存回退时隐藏骨架屏，避免旧内容与骨架叠加。
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

**扫掠光束与扫光验收（2026-08-25 补充）**：无头 Chrome 实测氛围层 3 束 `.tech-beam`
（`animationName=tech-beam-sweep`、10s、错峰 0.8/4.2/7.5s、`playState=running`、斜切矩阵
`matrix(1,0,-0.2867,1,*,0)` ≈ `skewX(-16deg)`）；暂停到可见相位截图可见贯穿背景的斜向
亮带；<768px `display:none`；`prefers-reduced-motion` 时长收敛 `0.01ms`、迭代 1 次；
主按钮 `::after` 的 `btn-sheen` 扫光保持 4s 运行；320/390/768/769/1024/1440 六档
`scrollWidth == innerWidth` 无横向溢出；预览截图已更新。

**真实凭据端到端与布局/骨架修复（2026-08-25 补充）**：用真实学号跑通成绩/课表/分析/PDF
四链路（成绩 `success=true,kind=grades_empty`，课表为学校端未开放的分类报告，分析/PDF 正常）；
修复两处前端缺陷：① 结果态容器由 `max-w-5xl` 放宽为 `max-w-7xl`（1280/1440 视口结果列
实测 612px→868px，表格 808px 无横向滚动）；② 缓存回退未隐藏骨架屏、提交未清除旧渲染定时器
（实测修复后 loading 期骨架可见、完成后隐藏，回退结果正常渲染）；六档视口无横向溢出。

**浏览器批注十一修（2026-08-26 补充）**：修复服务状态条颜色异常——JS 渲染成功段类名为
`status-seg success`，CSS 却写成 `.status-seg.ok` 导致成功段一直显示灰色；已对齐为
`.status-seg.success`（实测成功段 `rgb(42,124,82)` 绿、失败段红、空段灰）。

**浏览器批注十修（2026-08-26 补充）**：居中不改变宽度——`.layout` 补 `width:100%`
（受 `max-width:680px` 约束），flex 居中不再把表单收缩到内容宽度（1440 实测表单宽 680px、
上下间距 69/69px）。

**浏览器批注九修（2026-08-26 补充）**：查询表单组件在页面居中放置——首页态 `.wrap`
改为纵向 flex、`.layout:not(.has-result)` 用 `margin-block:auto`，表单垂直居中于标题与
服务状态之间（709 实测上下间距均 50px，1440 为 69/68px，水平左右 24px 居中）。

**浏览器批注八修（2026-08-26 补充）**：服务状态组件进一步取消框线（实测 `border:0px`、
背景透明、`box-shadow:none`、圆角 0，仅保留标题/进度条/统计文字）。

**浏览器批注七修（2026-08-26 补充）**：服务状态组件取消背景色（`bg-surface`→透明）与阴影，
仅保留边框/文字（实测 `background:rgba(0,0,0,0)`、`box-shadow:none`、边框 1px；
qwen3-vl-flash 复验无突出背景色块）。

**浏览器批注六修（2026-08-26 补充）**：成绩选项组件（仅生成 PDF / 分析成绩）改为**左对齐**
（`align-items:flex-start`、复选框与文字 `text-align:left`）；结果表格补齐内框线——
列间加纵向分隔线（末列除外），并修复模板 `last:border-b-0` 造成的每行末格缺下边框
（非末行末格补 `border-bottom`），实测每格右/下边框 1px 成完整网格，qwen3-vl-flash 复验
「横向与纵向分隔线齐全」。

**浏览器批注五修（2026-08-26 补充）**：移除返回查询按钮内未渲染的 SVG 图标（其 gap 使文字
偏右 4px），文字精确居中（实测文字中心偏移 0px、`justify-content:center` + `text-align:center`，
qwen3-vl-flash 复验居中无多余占位）。

**浏览器批注四修（2026-08-26 补充）**：结果卡头部标题与返回查询按钮**同一水平线**对齐
（隐藏标题装饰线并清零无层 `margin-bottom`，实测标题中心=按钮中心=175px，diff=0），
按钮文字 `justify-content:center` + `text-align:center` 居中；qwen3-vl-flash 复验基线对齐。

**浏览器批注三修（2026-08-26 补充）**：结果卡头部改为「返回结果」标题**左上**、
「返回查询」按钮**右上**（按钮文字居中，实测标题距卡左 29px、按钮距卡右 29px、text-align:center）；
页脚恢复 `body{min-height:100dvh}` + `site-footer{margin-top:auto}` **贴紧视口最下**
（709 视口实测页脚顶 y=806、视口高 863，qwen3-vl-flash 复验贴底）。

**浏览器批注二修（2026-08-26 补充）**：返回查询按钮改为结果卡顶部「返回结果」标题下方
**居中**主色按钮（图标+文字，实测按钮中心 355px=卡片中心 355px）；去除页面底部大片空白——
取消 `body{min-height:100dvh}`（页脚不再被顶到视口底部、改为紧跟内容，对齐原版普通流页脚）
并把 page-shell 底部留白 96px 收敛为 16px（实测服务状态→页脚间距 194px→16px）。

**浏览器批注修复（2026-08-26 补充）**：页头标题放大为 30px（移动 20px）解决「信息太小」；
「返回结果」标题降为 16px 解决「文字过大」；返回查询改为左上角显眼主色按钮（图标+文字，
网格居中标题、移动端堆叠）解决「位置不明显」；去掉结果卡 `min-height:min(72vh,880px)`，
卡片贴合内容（709 视口实测卡高 344px、页面无纵向滚动）解决「空位太大」。

**全屏结果与公开链接（2026-08-26 补充）**：查询后返回结果占满全屏（隐藏表单、单列全宽、
`min-height:min(72vh,880px)`）+「返回查询」按钮（实测点击后表单恢复、聚焦学号）；免密登录
`/jump/go` 与课表下载 `/get_schedule/export` 链接改为公网入口地址（compose 注入
`JWXT_PUBLIC_URL=${PUBLIC_BASE_URL}`），nginx 放行两路径并反代到 format-service，
format-service 新增白名单透传端点（经 internal 网络原样转发上游 HTML/文件响应）；
真实凭据实测跳转链接变为 `http://127.0.0.1:8000/jump/go?code=..` 且浏览器可打开桥接页，
无效下载码透传返回上游友好错误；新增 3 个透传用例（30/30 通过）。

**原版布局回对齐（2026-08-26 补充）**：按原版 dify-workflow-api 页面回对齐布局——
`.wrap` 上限 1024px→1600px、`.layout` 增加 `align-items:start`（结果卡按内容高度、不再拉伸等高）、
结果卡 sticky 恢复（无层 `.project-card` 的 `position:relative` 曾覆盖层内 sticky，已用无层
`#resultCard` 修复）；骨架屏改回原版文档式结构（60% 标题条 + 5 条 46px 全宽行，1.5s shimmer），
并保留 `hidden` 属性切换。playwright-cli 实测：1440 视口结果列 1028px、卡片高 311px、
`position:sticky` 滚动后停在 `top:16px`、两栏顶部同为 y=116、加载完成后骨架 `display:none`；
qwen3-vl-flash 复验内容紧凑无残留骨架条。注：左侧表单卡 `.card-halo` 的 18px 呼吸辉光会使其
视觉上沿略高，属既定光效，非布局偏移。

**骨架屏与结果对齐修复（2026-08-26 补充）**：定位到视觉刷新模块 `.search-skeleton` 为无层
`display:flex`，覆盖了 Tailwind 层内 `.hidden` 类，导致加载完成后骨架屏仍占位 236px 并把结果
内容挤到卡片下方（视觉模型复验证实标题与内容间残留灰色条）。修复为按子模块契约使用 `hidden`
属性切换（`.search-skeleton[hidden]{display:none}`），并把本页每行两根骨架条改为 58%/88%
（模板 `:last-child=34%` 面向三根条布局）；实测加载期 `display:flex` 且条宽 305.9/464.1px，
完成后 `display:none`、结果内容回到标题下方（h2 结束 197 → 正文起点 217），
playwright-cli 截图 + qwen3-vl-flash 复验无残留骨架条与错位；六档视口无横向溢出。

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
