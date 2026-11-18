---
name: deploy
description: "仅在用户显式调用 /deploy 请求准备 CleanSightBackend 安装依赖或第三方二进制物料时使用。"
disable-model-invocation: true
---

# CleanSight 依赖安装

仅在用户显式输入 `/deploy <任务>` 时启用。普通讨论和语法引用不触发；用户补充信息可继续用于同一任务。

## 输入与边界

目标默认当前机器与项目目录；远程安装由用户提供访问方式和目录。需要下载二进制时，使用用户本次或当前任务中已提供的源机地址；未提供则先询问，不内置默认地址。预检获取 OS、架构、Python、加速器，无法判断则先询问。Python 范围为 3.10–3.12，二进制归档支持 Linux／Windows x86_64。

本 skill 仅准备与验证依赖环境，不选择 dev／prod、不读取或生成 `.env*`、不配置或探测服务端口、不启动服务、不连接业务数据库、不执行端到端测试。模型准备与运行配置留给后续任务。

## 路由

- Linux NVIDIA 先读 [Linux 预检](references/linux.md)，Windows NVIDIA 先读 [Windows 预检](references/windows.md)，再按 [NVIDIA 安装](references/nvidia.md) 执行。
- PPU 读 [PPU 安装](references/ppu.md)，保护厂商包；平台包缺失或不兼容时停止报告。
- 准备或更新源机第三方二进制读 [物料维护](references/materials.md)。
- Python 安装后共用 [OpenCV 修复及验证](references/common.md)。设备计算验证留在平台 reference。

运行配置的历史说明仍保留在 `references/runtime-config.md`，本轮安装不执行该流程。

## 流程

1. 确认目标并按平台预检。命令在项目根目录执行。已有 `.venv` 先检查解释器路径、3.10–3.12 范围和 `pyvenv.cfg` 的系统包继承方式，NVIDIA 必须 false，PPU 必须 true。不匹配时停止并说明，询问如何处理现有环境。
2. 按平台安装 Python 依赖。普通包默认清华镜像，PPU 默认阿里镜像；访问失败时查明错误再选可用来源，不擅自改变依赖版本。无需无条件升级 pip。
3. 按 common reference 检查 OpenCV，仅不健康时修复。
4. 根据 [物料维护](references/materials.md) 确认用户提供的源机归档可用。使用目标 `.venv` 的 Python 执行 `python .claude/skills/deploy/scripts/install_binaries.py all --base-url "<源机地址>"`，替换占位符后执行。可只安装 `ffmpeg` 或 `mediamtx`；可用的既有二进制无需重装，此时无需提供源机地址。
5. 使用同一 Python 执行 `python .claude/skills/deploy/scripts/verify_environment.py`，再执行平台设备计算检查。
6. 报告环境验证结果、未完成项和真实执行范围；不要据此宣称业务服务可用。

通用工具不导入应用模块。下载、解压、命令或验证失败即停止对应步骤，不继续覆盖可用环境。辅助脚本同时执行 `all` 时逐组件提交，第二个组件失败会保留第一个已安装的组件，修复后单独重试失败组件。
