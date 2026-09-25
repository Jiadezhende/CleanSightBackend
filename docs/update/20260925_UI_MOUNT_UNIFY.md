# 静态资产统一挂到 `/ui-f3m8`：admin / lab 两页 + 共用 vendor

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

`app/static` 由一个挂载 `/ui-f3m8` 整体出：admin 页 `/ui-f3m8/admin/`、lab 页 `/ui-f3m8/lab/`、前端库 `/ui-f3m8/vendor/`。
两页各自的 `vendor/` 合并为 `app/static/vendor/` 一份。旧页面 URL `/admin-f3m8/ui/`、`/lab-f3m8/ui/` 失效，不留跳转；API 前缀不变。

## 变更背景

- **现状**：`/admin-f3m8/ui`、`/lab-f3m8/ui` 两个挂载各带一份 `vendor/`，其中 `vue.global.prod.js` / `element-plus.full.js` / `element-plus.css`
  字节完全相同（约 3.1 MB 重复）。admin 页离线推理 tab 要播放 HLS，需要 `hls.js`，而它只在 lab 那份里。
- **诉求**：运维页面无登录，靠路径里的混淆串挡随手访问；共享前端库的同时路径要短、且同样带混淆串。
- **推翻**：[20260903_PACKAGE_LAYOUT_SPEC](20260903_PACKAGE_LAYOUT_SPEC.md) 保留项「`app/static/` 的 vendor 不去重」。

## 方案详情

### 全景

```text
旧                                        新
/admin-f3m8/ui/            admin 页        /ui-f3m8/admin/
/lab-f3m8/ui/              lab 页          /ui-f3m8/lab/
/admin-f3m8/ui/vendor/x    admin 用的库     /ui-f3m8/vendor/x     两页共用一份
/lab-f3m8/ui/vendor/x      lab 用的库
```

| 部件 | 落在哪 |
|------|--------|
| 挂载 | `app/main.py`：两个 mount → `app.mount("/ui-f3m8", StaticFiles(directory=_STATIC_DIR, html=True))` |
| 前端库 | `app/static/{admin,lab}/vendor/*` → `app/static/vendor/`（5 个文件：3 个共有 + `chart.umd.js` + `hls.js`） |
| 页面引用 | 两个 `index.html` 的 `href` / `src` → `/ui-f3m8/vendor/...` |
| 引用新 URL | `README.md`、`docs/QUICK_START.md`、`docs/api/admin.md`、`docs/api/lab.md`、`integration_tests/README.md`、`integration_tests/test_{single,multi}_client.py` |

- `/ui-f3m8/admin` 由 `StaticFiles(html=True)` 重定向到 `/ui-f3m8/admin/` 并返回 `index.html`；`/ui-f3m8/` 根下无 `index.html`，返回 404，不列目录。
- 防护效果与改前相同：挡随手访问的是页面入口与 API 前缀里的混淆串；vendor 里都是公开开源库。
- `.gitignore` 的 `!app/static/**/vendor/` 已覆盖 `app/static/vendor/`，未改。
- `docs/kb/`（`ARCHITECTURE_API_SURFACE.md`、`SERVICE_LAB.md`）仍写旧 URL，留给 KB 融合。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 挂载点 | 2 个 | 1 个 |
| 前端库 | 两份，约 3.1 MB 重复 | 一份，两页共用 |
| 页面 URL | `/admin-f3m8/ui/`、`/lab-f3m8/ui/` | `/ui-f3m8/admin/`、`/ui-f3m8/lab/` |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_static_mount.py`（新） | 11 passed：两页 200、5 个库文件 200、旧页面 URL 404、页面不再引用 `/ui/vendor/` 且无私有 vendor 目录 |
| 全量 `pytest tests/` | 887 passed（基线 868 passed / 8 skipped，本机跳过项此次均跑通；+11 为新用例） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 旧书签 / 外部文档里的 `/admin-f3m8/ui/` | 打开 404 | 告知使用者新地址；不留跳转 |
| KB 两篇仍写旧 URL | KB 与代码暂时不一致 | 下次 KB 融合时按本记录替换 |
