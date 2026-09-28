# app/web —— 教学渲染内核宿主

内嵌页面由 `QWebEngineView` 加载，教学区、追问抽屉、小灶**三处共用**（见《技术文档》§4.8）。

| 文件 | 作用 |
| --- | --- |
| `lesson.html` | 渲染内核宿主页 |
| `renderer.js` | Markdown → 公式 → 代码高亮 → 图片 / SVG 注入 → 动画挂载 |
| `sandbox.html` | 动画沙箱宿主（iframe `srcdoc` + CSP） |
| `theme.css` | 主题 CSS 变量（与 Qt 侧 token 同源） |
| `vendor/` | 本地渲染依赖，**禁止 CDN**（见 `vendor/README.md`） |

安全：CSP `default-src 'none'`（禁网）；禁止加载远程资源；主题切换只改 CSS 变量，不重建 DOM。

> 本目录文件尚未创建，属 M0/M2 实现内容。
