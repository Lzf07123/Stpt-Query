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
| 认证表单卡 | `.card.card-signature.card-halo` | 签名描边 9s + 呼吸辉光 4.5s |
| 输入/下拉 | `.input`、`.dropdown` / `.dropdown-trigger` / `.dropdown-menu` / `.dropdown-item` | 44px 触达、焦点 ring 主色、键盘契约由既有 JS 提供 |
| 分段选择 | `.segmented` / `.seg-btn` | 查询项目（课表/成绩），active 半透明主色底 |
| 复选框 | `input[type=checkbox]` + `.field-check` | 18px、accent-color 主色 |
| 按钮 | `.btn` / `.btn-primary` / `.btn-ghost` / `.btn-sm` | 半透明单色 + 扫光 + 涟漪 |
| 结果卡 | `.project-card`（液态玻璃） | hover 上浮 3px、顶部主色细线展开 |
| 加载骨架 | `.search-skeleton` 结构（`#skeleton` 内） | shimmer 1.35s |
| 空状态 | `.empty-state` / `.empty-state-art` / `.empty-state-text` | SVG 小图 + 引导文案 |
| 提示/状态 | `.notice` 变体、`.status`（ok/error）、`.status-dot` | 语义色只表状态 |
| Toast | `.toast` / `.toast-success` / `.toast-error` / `.toast-info` | 进度条 + 进入/离开动画 |
| 服务状态 | `.service-status` / `.status-seg` / `.status-bar` | 最近 100 次查询可用性 |
| 页脚 | `.site-footer` / `.site-footer-inner` / `.filing-icon-placeholder` | 56px 单行、muted 链接、brand.js 驱动 |
| 返回顶部 | `.back-to-top` | 纯锚点 44×44 |
| 氛围层 | `.tech-ambience--soft` + `.tech-grid` | 工作台 soft；移动端停用/减量 |

## 4. 页面模式：单页查询工具

- 顶部居中标题 + 主题切换；主区「查询表单卡 + 结果玻璃卡」双栏（≥1024px），
  窄屏单栏；底部服务状态条；右下角返回顶部；`site-footer` 贴底。
- 结果渲染：服务端 Markdown → 本地零依赖解析（表格/引用/列表/代码），
  成绩列三档语义着色（高/中/低）+ 课程类别徽章；移动端表格横向滚动。
- 查询流程状态：loading（spinner + 骨架）→ success（toast + 结果）/ failure（notice）。

## 5. 令牌校验与验收证据

- 仅槽位差校验：模板实例化后 `--eduquery-*` 令牌集合与模板逐值一致（70/70）；
  仅新增 5 个前缀对齐别名（`--eduquery-motion-*` / `--eduquery-ease-*`，
  与无前缀 `--motion-*` / `--ease-*` 同值）。
- 计算样式抽查（无头 Chrome，本地静态预览）：1440/769/390/320 四档
  `scrollWidth == innerWidth`；结果卡 `backdrop-filter: blur(18px) saturate(1.5)`、
  圆角 20px；`.back-to-top` 桌面 20/24、移动 14/16；`.theme-toggle` 44×44；
  页脚链接 muted（浅 `rgb(100,115,108)`）；移动端氛围光点 `display:none`。
- 明暗模式：浅 `html` 背景 `rgb(246,251,249)`、深 `rgb(58,63,69)`（D1 雾灰）。

## 6. 构建与验收

```bash
cd frontend
npm install            # tailwindcss + @tailwindcss/cli（仅构建期依赖）
npm run build          # src/index.css → static/style.css（--minify）
```

验收清单（BRAND.md §5）逐项核对；四档视口 375/768/1024/1440 无横向滚动；
`prefers-reduced-motion` 收敛单帧；`rg '{{PROJECT_PREFIX}}' frontend/src/index.css`
无占位符残留；`rg '#[0-9a-fA-F]{3,8}' frontend/static/index.html` 无硬编码颜色。
