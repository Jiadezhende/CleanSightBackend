# PR 合入 dev / main 时自动推送飞书群消息

> **变更状态**：生效中（2026-10-24）
> **知识库**：无需沉淀（CI 配置，不涉及后端运行时）

## 概述

新增 GitHub Actions workflow [notify-feishu-on-merge.yml](../../.github/workflows/notify-feishu-on-merge.yml)：base 为 `dev` 或 `main` 的 PR 合入后，向飞书自定义机器人推送一条文本消息（PR 号、标题、分支、作者、合入人、链接）。关闭但未合入的 PR 不推送。

## 变更背景

- GitHub 原生 webhook 的 payload 结构固定、不可模板化，飞书自定义机器人只接受 `{msg_type, content}` 格式，二者无法直连；因此在 Actions 里组装消息后 curl 推送。

## 方案详情

- **Secrets**：`FEISHU_WEBHOOK` 必填；`FEISHU_SECRET` 在机器人开启签名校验时填，未配置则不签名。
- **防注入**：PR 标题等字段经 `env` 传入脚本、`jq` 组装 JSON，不在 `run:` 里直接插值 `${{ }}`。
- **失败判定**：飞书出错时 HTTP 仍返回 200，脚本校验响应 `code == 0`，否则 job 失败。
