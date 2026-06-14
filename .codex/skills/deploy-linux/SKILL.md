---
name: deploy-linux
description: 用于将 CleanSightBackend 部署到本地或远程 Linux x86_64 NVIDIA GPU 主机，包括仓库传输、SSH 免密、BASE_URL 离线安装、环境配置、后端启动、网关启动和端到端验证。
---

# Linux 部署工作流

## 用途

这个 skill 用于把 CleanSightBackend 部署到 Linux x86_64 + NVIDIA GPU 机器上。
它是 `.claude/skills/deploy-linux/SKILL.md` 的 Codex 中文转换版。原始文件是
更完整的 runbook；当需要精确命令或排查细节时，仍然可以回读原始文件。

## 触发场景

当用户提到以下任务时，应该先阅读本文件：

- Linux 部署、本地部署、远程部署、部署到服务器
- 把仓库传到服务器，绕过 GitHub 慢速 clone
- 配置 SSH 免密
- 离线安装、`install.sh`、`BASE_URL`、wheelhouse
- 启动后端、启动 RTSP 网关、验证 `/health/status`
- 跑 `integration_tests/test_single_client.py` 做端到端验证

Windows 开发环境不走这个 skill。

## 事实源

开始部署前优先查看：

- `DEPLOYMENT.md`：权威部署说明。
- `deploy.conf`：物料 URL、hash、`TORCH_PKGS`、`BASE_URL` 等版本信息。
- `install.sh`：Linux 生产安装入口。
- `.claude/skills/deploy-linux/SKILL.md`：原始详细部署 runbook。

## 每次部署前需要向用户确认

不要假设这些值：

- 目标机器 SSH 地址或别名。
- 物料源机 SSH 地址或别名。
- `BASE_URL`，例如 `http://<源机IP>:<端口>`。
- 源机上的分发服务名，例如 `cleansight-dist`。
- 目标数据库和告警端点是否允许写入测试数据。
- `.env` 或 `.env.dev` 里的 DB、告警 URL、网关配置等运行时参数。

## 硬性要求

- 目标系统必须是 Linux x86_64。
- `python3 -V` 必须解析到 Python 3.10。
- 目标机必须有 NVIDIA GPU。
- 安装完成后 `torch.cuda.is_available()` 必须为真。
- `ensurepip` 必须可用，否则无法正常创建 `.venv`。

## 总体流程

1. 判断目标机上是否已经有仓库。
2. 远程部署时先配置 SSH 免密。
3. 使用 `git archive` + `scp` 传输干净的版本控制快照。
4. 在目标机做预检：架构、Python、ensurepip、GPU、curl、磁盘、`BASE_URL` 连通。
5. 确认源机物料分发服务在线。
6. 使用 `BASE_URL=<BASE_URL> ./install.sh` 后台安装，并轮询 `install.log`。
7. 创建或检查 `.env` / `.env.dev`。
8. 启动后端并检查 `http://localhost:8000/health/status`。
9. 在 `.venv` 中启动 `mediamtx_gateway`，确认 `8004` 和 `18004` 都在监听。
10. 经用户确认后再运行端到端测试。

## 常用命令

传输仓库快照：

```bash
git archive --format=tar.gz -o /tmp/cleansight.tar.gz <分支名>
scp /tmp/cleansight.tar.gz cleansight-deploy:~/
ssh cleansight-deploy 'mkdir -p ~/CleanSightBackend && tar xzf ~/cleansight.tar.gz -C ~/CleanSightBackend && rm ~/cleansight.tar.gz'
```

目标机预检：

```bash
ssh cleansight-deploy 'uname -m; python3 -V; python3 -c "import ensurepip; print(\"OK\")"; nvidia-smi; command -v curl; df -h ~'
ssh cleansight-deploy 'curl -m 8 -sI <BASE_URL>/wheelhouse/SHA256SUMS | head -1'
```

后台安装：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && rm -f install.log && nohup env BASE_URL=<BASE_URL> ./install.sh > install.log 2>&1 & echo "PID: $!"'
```

查看安装进度：

```bash
ssh cleansight-deploy 'pgrep -f "[i]nstall.sh" >/dev/null && echo RUNNING || echo DONE; tail -20 ~/CleanSightBackend/install.log'
```

启动后端：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && chmod +x start_backend.sh && nohup ./start_backend.sh prod > backend.log 2>&1 & echo "PID: $!"'
ssh cleansight-deploy 'curl -s -m 5 http://localhost:8000/health/status; tail -5 ~/CleanSightBackend/backend.log'
```

启动 RTSP 网关：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && source .venv/bin/activate && nohup python -m mediamtx_gateway.main > gateway.log 2>&1 & echo "PID: $!"'
ssh cleansight-deploy 'sleep 4; ss -ltn | grep -E ":8004|:18004"'
```

端到端验证：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && source .venv/bin/activate && CLEANSIGHT_ENV=<prod|dev> python integration_tests/test_single_client.py --scenario 1 --task_id 1 --duration 30 --no-window'
```

## 常见坑

- 健康检查端点是 `/health/status`，不是裸 `/health`。
- `test_single_client.py` 默认按 dev 加载 `.env.dev`，生产环境需要显式设置 `CLEANSIGHT_ENV=prod`。
- 如果创建 venv 失败，通常需要安装 `python3.10-venv`，并删除失败遗留的 `.venv`。
- `start_backend.sh` 可能缺执行位，需要 `chmod +x`。
- 远程 `pkill -f` 使用 `[a]pp.main`、`[u]vicorn` 这类写法，避免命令匹配到自己。
- 网关必须在 `.venv` 里启动。
- 只看 `8004` 不够，应该同时确认 `8004` 和 `18004`。
- `nohup` 和 `tmux` 都不抗机器重启，需要开机自启时应改成 systemd。
- 不要编造 DB 凭据、告警 URL 或 `BASE_URL`，需要向用户确认。

## Codex 转换说明

这个 skill 没有必须依赖 Claude 专属工具的步骤。所有操作都可以转换为普通 shell
命令。但是远程 SSH、安装软件和端到端测试都有副作用，执行前需要确认目标环境和写入权限。
