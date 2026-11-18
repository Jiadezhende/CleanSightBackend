# 前置依赖安装迁入显式 deploy skill

> **变更状态**：生效中（2026-11-18）
> **知识库**：待沉淀

## 概述

移除根目录整套安装脚本，由显式 `/deploy` 编排平台依赖准备与环境验证。Python 范围统一 3.10–3.12，Python 包在线安装，第三方二进制继续源机分发。

## 方案详情

安装先按平台预检并复用兼容的项目虚拟环境，再安装 Python 包、按需修复 headless，安装二进制并完成环境与设备检查。安装不读取或生成环境文件、不配置端口、不启动服务、不连接业务库。

NVIDIA 的 PyTorch 2.8.0／torchvision 0.23.0 cu128 版本在 requirements/gpu.txt 显式声明，NVIDIA reference 集中维护安装索引命令；PPU 清单注明必需的厂商包，通过系统包继承和 NumPy 约束使用。基础验证与平台设备检查分别维护。

二进制辅助工具从用户提供的源机下载对应 Windows／Linux x86_64 包，不内置源机主机或端口；当前任务缺少地址时在下载前询问。临时解压并执行版本命令后发布；失败时保留或恢复旧安装。每个组件单独提交，all 的后续组件失败不会撤销前一个已成功组件。

`build.sh` 删除 wheelhouse 构建，保留四个已约定二进制归档的准备。MediaMTX 安装目录改为 `.mediamtx`，配置移动到 `config/mediamtx.yml`，LICENSE 保留原源码目录。网关默认路径和配置引用随迁移更新，显式二进制路径覆盖继续可用。

## 兼容与迁移

根目录 install.sh／install.ps1、requirements/prod.txt 已移除。使用显式 `/deploy` 安装，NVIDIA 共用 requirements/gpu.txt。旧 MediaMTX 二进制目录需通过新工具重新安装，不自动读取应用配置或迁移用户的自定义路径。旧源机 wheelhouse 不再使用，本轮不删除远程文件。

本轮保留现有运行环境文件命名、端口、reload、运行角色和进程控制；唯一运行路径改动为已授权的 MediaMTX 迁移。运行配置的历史 reference 暂存，后续独立 skill 未创建。

## 验证与限制

- 项目 Python 3.11 环境下，临时 ZIP／tar 归档、失败保留及发布回滚、网关配置／路径和既有网关测试共 24 项通过。
- 辅助脚本编译与帮助、build.sh 的 bash -n、Git 差异空白检查通过。
- Claude skill YAML 和本地 Markdown 链接单独检查；通用 skill 校验器不支持 Claude 专用 disable-model-invocation 字段，保留该显式调用开关。
- 测试进程屏蔽 `.env*` 文件存在性，并通过环境变量提供不可达占位连接信息。网关 socket 测试使用临时本机端口，未启动业务服务。
- 本轮未下载真实物料、重装依赖或执行 GPU／PPU 计算。合成测试对二进制执行使用 mock，各平台真实归档兼容性与 Python 3.10／3.12 组合尚待验证。
