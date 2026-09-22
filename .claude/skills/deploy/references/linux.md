# Linux + NVIDIA：install.sh 路径（生产、备用机、Linux 开发机通用）

本地机和远程机都走这一套，差别只在「要不要传仓库 + 配免密」。角色（prod / dev / test）只影响 [4]、[5] 两步。

## 固定约束

| 项 | 值 |
|----|----|
| 系统 / 架构 | Linux x86_64 |
| Python | **`python3 -V` 精确 3.10**。wheelhouse 按 cp310 打标签，版本不符 `install.sh` 启动即退出；多 Python 环境下脚本检的是 PATH 上的 `python3`，不是旁装的 `python3.10` |
| GPU | NVIDIA 驱动就位，`torch.cuda.is_available()` 为真；不需装 CUDA toolkit |
| 网络 | 能访问源机 `BASE_URL` 与清华 PyPI 镜像 |
| 工具 | `curl`、`python3 -m venv`（需要 `ensurepip`，Ubuntu 默认不带，见坑 1） |

## 总体流程

```text
[0] 仓库已在目标机？否则远程传（§1、§2）
[1] SSH 免密（远程才需要）
[2] git archive + scp 送仓库（绕开 GitHub 限速）
[3] 目标机预检
[4] 确认源机分发服务在线
[5] nohup 跑 install.sh，轮询日志
[6] 写 .env*（runtime-config.md）
[7] 端口空闲 → 启动 → /health/status
[8] e2e（有副作用，先问）
```

---

## [1] SSH 免密（远程部署）

输密码这步必须用户在自己终端做，非交互执行会卡。先查本地有没有 `~/.ssh/id_ed25519`，没有再 `ssh-keygen -t ed25519`。`~/.ssh/config` 加别名：

```
Host cleansight-deploy <目标IP>
  HostName <目标IP>
  User ubuntu
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 4
```

让用户执行 `ssh-copy-id -i ~/.ssh/id_ed25519.pub cleansight-deploy`，之后 `ssh cleansight-deploy 'echo ok'` 不要密码即成功。

## [2] 把仓库送上目标机

不要让目标机 `git clone` GitHub（出境常被限到 ~17 KiB/s），也不要 `tar` 整个工作树（HLS 录像、`.venv`、`__pycache__` 几百 MB 全是垃圾）。用 `git archive` 只导出版本控制的文件：

```bash
# 本地
git archive --format=tar.gz -o /tmp/cleansight.tar.gz <分支名>
scp /tmp/cleansight.tar.gz cleansight-deploy:~/
# 目标机
ssh cleansight-deploy 'mkdir -p ~/CleanSightBackend && tar xzf ~/cleansight.tar.gz -C ~/CleanSightBackend && rm ~/cleansight.tar.gz'
```

`git archive` 出来的不是 git 仓库，目标机不能 `git pull`。后续要在目标机拉更新，改用 `git bundle create x.bundle <分支>` + `git clone x.bundle`。

## [3] 目标机预检

一条 SSH 全查，全绿再往下：

```bash
ssh cleansight-deploy 'echo "== OS/arch =="; uname -m; . /etc/os-release && echo "$PRETTY_NAME"
echo "== python3 (须 3.10) =="; python3 -V
echo "== ensurepip =="; python3 -c "import ensurepip; print(\"OK\")" 2>&1 | tail -1
echo "== GPU =="; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
echo "== curl =="; command -v curl >/dev/null && echo OK || echo MISSING
echo "== disk =="; df -h ~ | tail -1
echo "== 源机连通 =="; curl -m 8 -sI http://49.234.120.241:8088/wheelhouse/SHA256SUMS | head -1'
```

## [4] 确认源机分发服务在线

预检里源机返回 `200` 即已开。超时则去源机启（服务名、别名见 [materials.md](materials.md)）：

```bash
ssh label-studio 'sudo systemctl start cleansight-dist && systemctl is-active cleansight-dist'
```

## [5] 跑 install.sh

torch + 全套 CUDA wheel 走 HTTP 流式拉（几个 GB、`--no-cache-dir` 不落盘），耗时 20–40 分钟。nohup 后台 + 写日志 + 轮询，别在前台干等：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && rm -f install.log \
  && nohup ./install.sh > install.log 2>&1 & echo "PID: $!"'   # 换源机才加 env BASE_URL=http://...
```

```bash
ssh cleansight-deploy 'pgrep -f "[i]nstall.sh" >/dev/null && echo RUNNING || echo DONE; \
  grep -E "^\[[0-9]/3\]|验证安装|ERROR|CUDA OK" ~/CleanSightBackend/install.log | tail; \
  tail -3 ~/CleanSightBackend/install.log'
```

脚本依次做：建 `.venv` → 从源机 wheelhouse 流式装 torch 闭包（`--require-hashes` 逐 wheel 校 SHA）→ 清华源装 `requirements/prod.txt` → 修 opencv-headless 冲突并把 numpy 钉回 `1.26.4` → ffmpeg 到 `.ffmpeg/` → mediamtx 到 `mediamtx/` → 自检。成功标志是日志末尾：

```
torch 2.8.0... | numpy 1.26.4 | cv2 ... | ultralytics ... | CUDA OK
ffmpeg version ...
mediamtx ... v1.15.5
```

安装产物：

```text
.venv/
.ffmpeg/bin/ffmpeg
mediamtx/mediamtx          # 只更新二进制；同目录 mediamtx.yml 随仓库走，不覆盖
```

## [6] 写 .env*

见 [runtime-config.md](runtime-config.md)。prod 用 `.env`，dev 用 `.env.dev`，test 用 `.env.test`。三个文件都在 `.gitignore` 里，`git archive` 不传，目标机要自己建。DB 凭据、告警 URL 找用户要，不要瞎填。

## [7] 启动 + 验证

先确认启动脚本声明的五个端口在本机空闲（默认值与查法见 runtime-config.md）。被占时不会在安装自检里暴露，只在启动时失败，且 MediaMTX 的绑定失败落在网关日志、不在后端日志。

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend \
  && nohup ./start_backend.sh prod > backend.log 2>&1 & echo "PID: $!"'   # dev / test 换参数
```

启动脚本一条命令拉起网关（网关再拉起 MediaMTX）+ 后端。等约 20 秒（建连接池 + 预热 YOLO），在服务器本地 curl（网关 IP 白名单可能挡外部）：

```bash
ssh cleansight-deploy 'pgrep -f "[a]pp.main" >/dev/null && echo "PROC UP" || echo DOWN; \
  curl -s -m 5 http://localhost:8000/health/status; echo; \
  ss -ltn | grep -E ":8004|:18004"; tail -5 ~/CleanSightBackend/backend.log'
```

成功标志：日志 `Application startup complete.`，`/health/status` 返回 `{"status":"running",...}`，**8004 与 18004 都在听**（只看 8004 会漏判：MediaMTX 没起时 8004 能 accept 但转发失败）。

常驻方式：nohup 或 tmux（后端一个会话），两者都不抗重启，要开机自启得配 systemd。换 tmux 前先用坑 6 的括号技巧停掉 nohup 进程。

## [8] 端到端验证

有副作用：会向 DB 建测试任务、推流跑推理、可能向告警端点发请求。目标库或告警端点是共享资源时**先问用户**。测试正常结束会自动删自己建的任务，中途异常退出可能留残留。

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && source .venv/bin/activate \
  && CLEANSIGHT_ENV=<prod|dev|test> python integration_tests/test_single_client.py \
       --scenario 1 --task_id 1 --duration 30 --no-window'
```

`CLEANSIGHT_ENV` 必须显式给且与启动时一致，见坑 5。前置：`/health/status` 可达、8004 与 18004 在听、`integration_tests/fixtures/test_video.mp4` 存在、目标 DB 可写。通过标志逐条出现：

```
✅ 创建测试任务 1
✅ ffmpeg RTSP 推流已启动: rtsp://127.0.0.1:8004/live/test.s1
/api/start 成功: {'status': 'success', ...}
terminate 结果: success
✅ 清理测试任务 1
Scenario 1 完成
```

从另一台机器验远程后端加 `--server <目标机IP>`，端口非默认再加 `--api-port` / `--rtsp-port`。

---

## 已知坑

### 坑 1：`ensurepip is not available`，建 venv 失败

Ubuntu 默认不装。修完清掉坏 `.venv` 再重跑 [5]：

```bash
ssh cleansight-deploy 'sudo apt-get update -qq && sudo apt-get install -y python3.10-venv'
ssh cleansight-deploy 'rm -rf ~/CleanSightBackend/.venv'
```

### 坑 2：源机 wheel 被 pip 忽略（`not a trusted or secure host`）

pip 对明文 HTTP 的 `--find-links` 默认当不可信主机忽略，叠加 `--no-index` 后找不到任何 wheel，报错包名随机。当前 `install.sh` 已按 `BASE_URL` 的 host 加 `--trusted-host`；在旧副本上撞到就同步一份新脚本过去。

### 坑 3：`python3` 不是 3.10

见固定约束。旁装了 3.10 也没用，得让 PATH 上的 `python3` 解析到它。

### 坑 4：`start_backend.sh: Permission denied`

仓库里 `start_backend.sh` 曾经没打执行位（2026-09-22 已 `git update-index --chmod=+x` 修掉）。拿到的是旧快照就 `chmod +x *.sh`。

### 坑 5：e2e 测试默认按 `dev` 找 `.env.dev`

`test_single_client.py` 与 `app.settings` 默认 `CLEANSIGHT_ENV=dev`。只部署了 prod 时测试在 `Settings()` 构造时就抛 `ValidationError`，发生在连 DB 之前、无副作用。修：命令前置 `CLEANSIGHT_ENV=prod`。

### 坑 6：SSH 里 `pkill -f "<模式>"` 连自己一起杀

远程命令行本身含模式字符串，会被匹配，SSH 退出码 255、同一条命令里 pkill 之后的步骤全不执行。用括号技巧：

```bash
pkill -f "[a]pp.main"; pkill -f "[u]vicorn"
pkill -f "[m]ediamtx_gateway"; pkill -f "[m]ediamtx/mediamtx"   # 先杀网关，否则它会自动重启 mediamtx
sleep 2; ss -ltn | grep -E ":8000|:8004|:18004" || echo "全部已释放"
```

### 坑 7：`/health` 裸端点是 404

健康路由 `prefix="/health"` 下没有裸 `/health`，实际是 `/health/status`。

---

## 部署完成检查清单

- [ ] 仓库已在目标机（archive，非整目录 tar）
- [ ] 预检全绿（py3.10 / GPU / ensurepip / curl / 源机 200）
- [ ] 源机分发服务在线
- [ ] `install.sh` 自检通过（torch+CUDA OK / ffmpeg / mediamtx）
- [ ] 对应角色的 `.env*` 已填（六项必填 + prod 的 `MEDIA_TOKEN_SECRET`）
- [ ] 五个端口空闲，`/health/status` 返回 `running`，8004 与 18004 都在听
- [ ] e2e 前已与用户确认可写测试数据，带 `CLEANSIGHT_ENV` 跑通
- [ ] 部署窗口结束，源机分发服务已关
