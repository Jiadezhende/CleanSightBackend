---
name: cleansight-codex-collaboration
description: CleanSightBackend 仓库内给 Codex 使用的协作说明，汇总从 .claude/skills 转换过来的项目工作流。
---

# CleanSightBackend Codex 协作说明

这个目录保存从 `.claude/skills/` 转换来的 Codex 版本工作流说明。原始
`.claude/skills/` 保持不动。

## 项目简介

CleanSightBackend 是一个围绕视频流、模型推理、时序分析、告警和持久化构建的
Python 后端项目。当前推理链路的核心组织方式是：

```text
Detector -> DetectionOutput -> slide_window -> TemporalAnalyzer -> events / alarms
```

其中 Detector 负责无状态帧级推理，TemporalAnalyzer 负责每个 client 独立的
时序分析、事件生成和告警生成。

## 重要目录

- `app/services/inference/`：推理管理器、worker、数据模型和 workflow 实现。
- `app/services/inference/workflows/`：Detector 和 TemporalAnalyzer 的具体实现。
- `config/inference_config.yaml`：stage、model、analyzer 的装配入口。
- `app/services/client/`：client 队列、滑动窗口和运行状态。
- `app/services/persistence/`：告警和结果持久化。
- `integration_tests/`：端到端 client 和推流测试。
- `.claude/skills/`：队友为 Claude 写的原始工作流说明。
- `.codex/skills/`：转换给 Codex 使用的项目内工作流说明。

## 常用命令

优先使用仓库里的虚拟环境：

```bash
source .venv/bin/activate
python -m pytest
```

常用的定向检查：

```bash
python -m pytest tests/test_mstcn_features.py tests/test_mstcn_phase_chain.py -q
python -m py_compile app/services/inference/workflows/*.py
python integration_tests/test_single_client.py --scenario 1 --task_id 1 --duration 30 --no-window
```

本地启动后端：

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Git 协作规则

- 修改前先查看 `git status`，避免把别人的改动当成自己的改动处理。
- 不要回退用户已有修改，除非用户明确要求。
- 提交前只 `git add` 本次任务相关文件。
- 如果误 `git add`，使用 `git restore --staged <path>` 或 `git restore --staged .`。
- 不要把运行产物、大文件、缓存和密钥文件混进提交。

## 不应提交的文件

除非用户明确确认，不要提交：

- `.venv/`、`__pycache__/`、`.pytest_cache/`、`.mypy_cache/`
- `*.log`、`backend.log`、`gateway.log`、`install.log`
- 运行时视频、HLS 分片、临时流文件
- checkpoints、训练输出、下载数据集、大压缩包
- `.env`、`.env.dev`、`.env.test`
- macOS 元数据文件，例如 `._*` 和 `.DS_Store`

如果项目里已经有被有意纳入版本管理的模型权重，需要按具体上下文判断；不要主动新增大模型文件。

## 如何使用这些 skills

当任务匹配某个 workflow 时，先阅读对应文件：

- `deploy-linux`：Linux 部署、离线安装、服务器传输、`install.sh`、后端和网关启动、部署验证。
- `infer-workflow`：新增推理 workflow、Detector、TemporalAnalyzer、滑动窗口、events、alarms、MS-TCN 接入。
- `schema-inspect`：检查 PostgreSQL 表结构，对比 ORM，识别无代码平台 hidden 字段。

这些文件是项目内说明。如果 Codex 没有自动加载它们，可以在对话里明确要求：

```text
请先阅读 .codex/skills/infer-workflow/SKILL.md，再帮我实现推理 workflow。
```

## Claude 专属内容的处理方式

原始 `.claude/skills/` 里可能出现类似 `/schema-inspect` 的 Claude 触发写法。
在 Codex 中不要把它当成可执行命令，而是把它理解为自然语言任务触发条件。

如果原始 skill 中出现当前环境不可用的 Claude 专属工具，需要转换为普通 shell、
Python、SQLAlchemy 或项目已有脚本；无法转换时要在回答里明确说明不能直接执行。
