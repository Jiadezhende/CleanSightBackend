---
name: deploy-linux
description: "Deploy CleanSightBackend to a local or remote Linux x86_64 GPU host (offline install via install.sh + BASE_URL). Covers repo transfer that bypasses slow GitHub, SSH passwordless setup, target pre-flight checks, running install.sh, known gotchas, .env config, startup and end-to-end verification. Use when: 部署、跑部署、远程部署、本地部署、Linux部署、部署到服务器、把仓库传到服务器、传仓库、配置免密、离线安装、跑install.sh、deploy、deploy to server、run deployment、offline deploy、install on server."
---

# CleanSight Linux 部署 Runbook

把 CleanSightBackend 部署到一台 **Linux x86_64 + NVIDIA GPU** 机器上,跑通离线安装到端到端验证。

> **适用范围**：只要目标是 **Linux**，无论本地还是远程都走这一套（差别仅在「要不要传仓库 + 配免密」这几步，远程才需要）。**Windows 部署不走本 skill**，见 [DEVELOPMENT.md](DEVELOPMENT.md) 的 Windows 路径。

## 事实源（先读，本 skill 不取代它们）

- [DEPLOYMENT.md](DEPLOYMENT.md) — 权威部署说明，变量含义、`install.sh` 内部行为以它为准。
- [deploy.conf](deploy.conf) — 钉版物料 URL/SHA、`TORCH_PKGS`、`BASE_URL`。**所有版本只在这里改。**
- [install.sh](install.sh) — Linux 安装入口（生产）。
- 记忆 `offline-bundle-distribution` / `deploy-docs-stale` — 离线物料分发的设计背景。

> 本 skill 是**操作手册 + 踩坑记录**：固化一条验证过的执行顺序，并标注几个文档里没写、但实跑会撞上的坑。

---

## 部署前先向用户确认（每次都问，不要假设）

物料源机是会变的共享资源——**别写死，开工前先问用户**。需要拿到的值，后文统一用这些占位符代入：

| 占位符 | 含义 | 怎么拿 |
|--------|------|--------|
| `<源机别名>` | 物料源机的 SSH 别名/地址（含登录用户） | 问用户；或看 `~/.ssh/config` 现有别名 |
| `<BASE_URL>` | 物料分发地址，形如 `http://<源机IP>:<端口>` | 问用户（注意源机上别的服务可能占了常用端口） |
| `<分发服务名>` | 源机上的 systemd 分发服务名（默认 `cleansight-dist`） | 问用户/源机 `systemctl list-units | grep dist` |

> 分发服务通常是 `static`、**不开机自启**，只在部署窗口手动开。部署命令即 `BASE_URL=<BASE_URL> ./install.sh`。

## 固定约束（与源机无关，不用问）

| 项 | 值 |
|----|----|
| 生产 Python | **必须 `python3 -V` == 3.10**（wheelhouse 按 cp310 打标签，强校验） |
| 硬件 | NVIDIA GPU，`torch.cuda.is_available()` 必须为真 |

---

## 总体流程

```
[0] 选传输方式：仓库已在目标机？ → 否则远程传
[1] SSH 免密登录（远程才需要）
[2] 把仓库送上目标机（git archive + scp，绕开 GitHub 限速）
[3] 目标机预检（py3.10 / GPU / venv / curl / 源机连通）
[4] 确认源机分发服务在线
[5] 跑 install.sh（BASE_URL 模式，nohup 后台 + 日志轮询）
    └─ 撞坑见「已知坑」一节
[6] 配置 .env（prod=.env / dev=.env.dev；DB / 告警 URL / 网关）
[7] 启动后端（chmod +x + nohup）+ 验 /health/status（本地 curl）
[8] 端到端 test_single_client
```

---

## [1] SSH 免密登录（远程部署）

目标机信息到手后，先配免密。**输密码这步必须用户在自己终端做**（交互式，非交互执行会卡）。

先查本地有没有现成密钥（`~/.ssh/id_ed25519`）。有就直接用，没有再 `ssh-keygen -t ed25519`。

在 `~/.ssh/config` 加别名（示例，IP/端口换实际值）：

```
Host cleansight-deploy <目标IP>
  HostName <目标IP>
  User ubuntu
  Port 22
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 4
```

让用户在**他自己的终端**执行（会提示输一次密码）：

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub cleansight-deploy
```

验证：`ssh cleansight-deploy 'echo ok'` 不再要密码即成功。

---

## [2] 把仓库送上目标机（绕开 GitHub 限速）

**不要让目标机 `git clone` GitHub**——国内/云机出境常被限到 ~17 KiB/s，40MB 要 40 分钟。
**也不要 `tar` 整个工作树**——运行时产物（HLS 录像、`__pycache__`、`.venv`）几百 MB 全是垃圾。

用 `git archive` 只导出版本控制的文件（≈38MB，等价 depth-1 快照）：

```bash
# 本地（开发机，网络好）
cd <本地仓库>
git archive --format=tar.gz -o /tmp/cleansight.tar.gz <分支名>   # 如 feat/deploy-opt
scp /tmp/cleansight.tar.gz cleansight-deploy:~/

# 目标机解包
ssh cleansight-deploy 'mkdir -p ~/CleanSightBackend && tar xzf ~/cleansight.tar.gz -C ~/CleanSightBackend && rm ~/cleansight.tar.gz'
```

实测 scp 约 25 秒（1.5 MB/s）传完，比 GitHub 快近百倍。

> **要不要 git 能力？** `git archive` 出来的**不是 git 仓库**，目标机不能 `git pull`。若后续要在目标机拉更新，改用 `git bundle create x.bundle <分支>`（≈87MB，带历史）+ `git clone x.bundle`。部署场景一般用 archive 就够。

---

## [3] 目标机预检（一次性脚本）

跑安装前先验硬约束，**别盲跑**。一条 SSH 全查：

```bash
ssh cleansight-deploy 'echo "== OS/arch =="; uname -m; . /etc/os-release && echo "$PRETTY_NAME"
echo "== python3 (须 3.10) =="; python3 -V
echo "== ensurepip (建 venv 需要) =="; python3 -c "import ensurepip; print(\"OK\")" 2>&1 | tail -1
echo "== GPU =="; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>&1
echo "== curl =="; command -v curl >/dev/null && echo OK || echo MISSING
echo "== disk =="; df -h ~ | tail -1
echo "== 源机连通 =="; curl -m 8 -sI <BASE_URL>/wheelhouse/SHA256SUMS | head -1'
```

全绿才往下走。任一不满足见「已知坑」。

---

## [4] 确认源机分发服务在线

`<分发服务名>` 是 `static`、手动启停。预检里 `curl <BASE_URL>/wheelhouse/SHA256SUMS` 返回 `200` 说明已开。
若超时/连不上，去源机启它：

```bash
ssh <源机别名> 'sudo systemctl start <分发服务名> && systemctl is-active <分发服务名>'
```

> 部署窗口结束后建议让管理员 `sudo systemctl stop <分发服务名>` 关掉公网暴露（明文无鉴权）。

---

## [5] 跑 install.sh

torch + 全套 CUDA wheel 是流式拉的（几个 GB、`--no-cache-dir` 不落盘），耗时 20–40 分钟。
**用 nohup 后台跑 + 写日志 + 轮询**，别在前台干等：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && rm -f install.log \
  && nohup env BASE_URL=<BASE_URL> ./install.sh > install.log 2>&1 & echo "PID: $!"'
```

轮询进度：

```bash
ssh cleansight-deploy 'pgrep -f "[i]nstall.sh" >/dev/null && echo RUNNING || echo DONE; \
  grep -E "^\[[0-9]/3\]|验证安装|ERROR|CUDA OK" ~/CleanSightBackend/install.log | tail; \
  tail -3 ~/CleanSightBackend/install.log'
```

`install.sh` 干的事（详见 DEPLOYMENT.md）：建 `.venv` → torch 闭包（HTTP 流式，`--require-hashes` 逐 wheel 校 SHA）→ 清华源装轻量依赖 → 修 opencv-headless + 锁 numpy==1.26.4 → ffmpeg→`.ffmpeg/` → mediamtx→`mediamtx/` → 自检。

**成功标志**——日志末尾出现：

```
torch 2.8.0... | numpy 1.26.4 | cv2 ... | ultralytics ... | CUDA OK
ffmpeg version ...
mediamtx ... v1.15.5
```

---

## 已知坑（文档没写、实跑会撞）

### 坑 1：`ensurepip is not available` / 建 venv 失败

`python3 -c "import venv"` 能过，但建 venv 需要 `ensurepip`，Ubuntu 默认不装。症状：

```
ensurepip is not available ... apt install python3.10-venv
```

修（免密 sudo 时直接做；否则让用户做）：

```bash
ssh cleansight-deploy 'sudo apt-get update -qq && sudo apt-get install -y python3.10-venv \
  && python3 -c "import ensurepip; print(OK)"'
ssh cleansight-deploy 'rm -rf ~/CleanSightBackend/.venv'   # 清掉失败留下的坏 .venv
```

然后重跑 [5]。

### 坑 2：HTTP 源机的 wheel 被 pip 忽略（`--trusted-host`）

症状（`install.sh` 在 torch 闭包那步整体失败，报错的包名随机）：

```
WARNING: The repository located at <源机IP> is not a trusted or secure host and is being ignored...
ERROR: No matching distribution found for <某个包>
```

成因：pip 对**明文 HTTP** 的 `--find-links` 默认当不可信主机忽略，叠加 `--no-index` → 找不到任何 wheel。
**当前 `install.sh` 已修复**：HTTP `BASE_URL` 时按 host 加 `--trusted-host`。若在**修复前的旧副本**上撞到，补丁是给 torch 那条 pip 命令加：

```bash
base_host="$(printf '%s' "$BASE_URL" | sed -E 's#^[a-z]+://([^:/]+).*#\1#')"
pip install --no-index --find-links "$torch_links" --trusted-host "$base_host" --no-cache-dir \
    --require-hashes -r "$sums_tmp/torch-reqs.txt"
```

改完同步文件再重跑：`scp install.sh cleansight-deploy:~/CleanSightBackend/install.sh`。

### 坑 3：`python3` 不是 3.10

`install.sh` 检的是 PATH 上的 `python3 -V`（不是旁装的 `python3.10`）。多 Python 环境下确保 `python3` 解析到 3.10，否则 wheelhouse 的 cp310 wheel 匹配不上、启动即退。

### 坑 4：`start_backend.sh: Permission denied`

`git archive` 按 git 里记录的 mode 还原文件，**脚本在仓库里没打执行位就解不出 +x**。症状：`nohup` 启动时
`failed to run command './start_backend.sh': Permission denied`，进程根本没起。补执行位即可：

```bash
ssh cleansight-deploy 'chmod +x ~/CleanSightBackend/start_backend.sh ~/CleanSightBackend/*.sh'
```

### 坑 5：`/health` 裸端点是 404

健康路由 `prefix="/health"` 下**没有裸 `/health`**，实际端点是 `/health/status`（另有 `/health/monitor/stats`、`/health/monitor/config`）。`curl .../health` 永远 404，别误判成没起来。

### 坑 6：e2e 测试默认按 `dev` 找 `.env.dev`，纯 prod 部署在连库前就崩

`test_single_client.py` / `app.settings` 默认 `CLEANSIGHT_ENV=dev` → 加载 `.env.dev`。若只部署了 prod（仅 `.env`），测试**在 `settings = Settings()` 构造时**就抛 pydantic `ValidationError`：

```
[Settings] Warning: Environment file '.env.dev' not found
pydantic_core._pydantic_core.ValidationError: 7 validation errors for Settings
  db_host  Field required ...
```

这发生在**连 DB 之前**，所以无副作用（不会写库），别慌。修：测试命令前置 `CLEANSIGHT_ENV=prod`（与启动后端的环境一致），或目标机存在 `.env.dev`。
> **`.env.dev` 在 `.gitignore` 里**（连同 `.env`、`.env.test`），`git archive` **不会传它**——dev 部署得在目标机自行创建 `.env.dev`（可照 `.env.example` 填，或把 [6] 的 `.env` 内容 `mv` 成 `.env.dev`）。

### 坑 7：SSH 里 `pkill -f "<模式>"` 会连自己那条命令一起杀

通过 SSH 远程 `pkill -f "app.main"` 时，**你这条远程命令行本身就含 `app.main`**（因为命令串里写了它），会被一并匹配 → SSH 退出码 **255**、**同一条命令里 pkill 之后的步骤全不执行**（典型：重启脚本只跑了「停」没跑「起」，服务停了起不回来）。
修：用**括号技巧**让正则不匹配自身（`[a]pp.main` 仍匹配进程的 `app.main`，但不匹配你命令行里的字面量 `[a]pp.main`）：

```bash
pkill -f "[a]pp.main"; pkill -f "[u]vicorn"          # 后端
pkill -f "[m]ediamtx_gateway"; pkill -f "[m]ediamtx/mediamtx"   # 网关 + 它托管的 MediaMTX（先杀网关，否则它会自动重启 mediamtx）
sleep 2; ss -ltn | grep -E ":8000|:8004|:18004" || echo "全部已释放"
```

---

## [6] 配置 .env（启动前必填）

安装只装依赖，**不碰运行时配置**。生产用 `.env`，启动脚本按 `CLEANSIGHT_ENV` 选文件。至少填：

```dotenv
CLEANSIGHT_DB_HOST=...
CLEANSIGHT_DB_PORT=5432
CLEANSIGHT_DB_NAME=...
CLEANSIGHT_DB_USER=...
CLEANSIGHT_DB_PASSWORD=...
CLEANSIGHT_FILE_PATH_INSERT_URL=...
CLEANSIGHT_ALARM_REPORT_URL=...
CLEANSIGHT_STRICT=1
```

建议显式配（否则重启失效/功能受限）：
```dotenv
CLEANSIGHT_MEDIA_TOKEN_SECRET=...      # 媒体 URL HMAC 签名密钥
CLEANSIGHT_MODEL_PATH=...              # 模型目录非默认 ./app/data 时
CLEANSIGHT_GATEWAY_ALLOWED_IPS=...     # 需限制 HTTP API 来源 IP 时
```

> DB 凭据、告警 URL 这些**我没有**——找用户要，或确认目标机上是否已有 `.env`。不要瞎填。

---

## [7] 启动后端 + 验证

启动脚本按参数选环境文件：`prod`→`.env`、`dev`→`.env.dev`、`test`→`.env.test`。
**先补执行位**（坑 4），再 `nohup` 后台跑 + 写日志（前台会被 SSH 断开杀掉）：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && chmod +x start_backend.sh \
  && nohup ./start_backend.sh prod > backend.log 2>&1 & echo "PID: $!"'   # dev 环境改 dev
```

等 ~20 秒（要建连接池 + 预热 YOLO 模型，约 2s），**在服务器本地 curl**
（`/health/status`，不是裸 `/health`——见坑 5；网关 IP 白名单可能挡外部，本地最稳）：

```bash
ssh cleansight-deploy 'pgrep -f "[a]pp.main" >/dev/null && echo "PROC UP" || echo DOWN; \
  curl -s -m 5 http://localhost:8000/health/status; echo; tail -5 ~/CleanSightBackend/backend.log'
```

成功标志：日志 `Application startup complete.`，`/health/status` 返回 `{"status":"running",...}`。

RTSP 网关另起一个进程，**必须在 venv 里跑**（依赖装在 `.venv`，裸 `python` 起不来），对外 RTSP 端口 8004。端到端测试（[8]）要推流到它，所以测试前一定要先把它拉起来：

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && source .venv/bin/activate \
  && nohup python -m mediamtx_gateway.main > gateway.log 2>&1 & echo "PID: $!"'
# 验证：8004（对外代理）和 18004（MediaMTX 内部）都要在听
ssh cleansight-deploy 'sleep 4; ss -ltn | grep -E ":8004|:18004"; \
  ss -ltn | grep -q :8004 && ss -ltn | grep -q :18004 && echo "GATEWAY UP" || { echo DOWN; tail -8 ~/CleanSightBackend/gateway.log; }'
```

> **网关会自带拉起 MediaMTX**：`mediamtx_gateway/config.ini` 默认 `mediamtx_bin = auto`，所以这一条进程同时起了 **MediaMTX（`127.0.0.1:18004`，仅本机）+ TCP 代理（`0.0.0.0:8004`）**，且随仓库的 `mediamtx/mediamtx.yml` 已把 `rtspAddress` 配成 `127.0.0.1:18004`（端口收敛已就位）。所以**只跑这一条就够**，不用再单独起 mediamtx。
> 反之若 `config.ini` 的 `mediamtx_bin` 留空（纯代理模式），8004 能 accept 但转发到 18004 会失败（没人监听）——这时才需要单独起 MediaMTX。验证以「**8004 和 18004 都在听**」为准，只看 8004 会漏判。

### 常驻方式：nohup 还是 tmux

上面用 `nohup`；若想之后能 attach 进去看实时日志，改用 **tmux**（后端、网关各一个会话）：

```bash
ssh cleansight-deploy "tmux new-session -d -s cleansight; \
  tmux send-keys -t cleansight 'cd ~/CleanSightBackend && ./start_backend.sh dev 2>&1 | tee backend.log' C-m; \
  tmux new-session -d -s cleansight-gw; \
  tmux send-keys -t cleansight-gw 'cd ~/CleanSightBackend && source .venv/bin/activate && python -m mediamtx_gateway.main 2>&1 | tee gateway.log' C-m; \
  tmux ls"
# 重连查看：ssh cleansight-deploy -t 'tmux attach -t cleansight'（脱离 Ctrl-b d）
# 抓取某会话当前输出（不 attach）：ssh cleansight-deploy 'tmux capture-pane -t cleansight -p | tail'
```

> **nohup 和 tmux 都不抗重启**：机器重启后两者都不会自动拉起（tmux 会话也随之消失）。要开机自启得配 systemd（后端 + `mediamtx_gateway` 各一个单元）。
> 换 tmux 前记得**先停掉已有的 nohup 进程**（用括号技巧的 `pkill`，见坑 7），否则端口被占、新会话起不来。

---

## [8] 端到端验证

依赖都齐还不够，跑一次单客户端集成测试验证推流→推理→告警→清理。

> **先确认再跑（有副作用，别擅自）**：此测试会向 DB **建一条测试任务**、推流跑推理、并可能向**告警端点**（`CLEANSIGHT_ALARM_REPORT_URL`）发请求——属对外、有副作用的动作。目标库/告警端点是共享或生产资源时，**务必先问用户**能不能写测试数据。测试正常结束会自动删掉自己建的任务（见下「通过标志」最后一行），但中途异常退出可能留残留。

```bash
ssh cleansight-deploy 'cd ~/CleanSightBackend && source .venv/bin/activate \
  && CLEANSIGHT_ENV=<prod|dev> python integration_tests/test_single_client.py \
       --scenario 1 --task_id 1 --duration 30 --no-window'
```

> **必须显式给 `CLEANSIGHT_ENV`**：测试进程默认按 `dev` 加载 `.env.dev`，纯 prod 部署（只有 `.env`）会在连库前直接崩——见坑 6。环境要与 [7] 启动后端时一致。

前置确认：后端 `/health/status` 可达、**RTSP 网关已在 venv 里起且 8004+18004 都在听**（见 [7]）、`test/test_video.mp4` 存在、目标 DB 可写。

**通过标志**（日志逐条出现）：

```
✅ 创建测试任务 1                                              # 任务不存在时自动建（source_ip=test.s1）
✅ ffmpeg RTSP 推流已启动: rtsp://127.0.0.1:8004/live/test.s1   # 用钉版 .ffmpeg/bin/ffmpeg
/api/start 成功: {'status': 'success', 'client_id': 'test.s1', ...}
运行中（无窗口，30s）...
terminate 结果: success
✅ 清理测试任务 1                                              # 自动删除，DB 恢复原状
Scenario 1 完成
```

---

## 部署完成检查清单

- [ ] 仓库已传到目标机 `~/CleanSightBackend`（archive，非整目录 tar）
- [ ] 预检全绿（py3.10 / GPU / ensurepip / curl / 源机 200）
- [ ] 源机分发服务（`<分发服务名>`）在线
- [ ] `install.sh` 自检通过（torch+CUDA OK / ffmpeg / mediamtx）
- [ ] `.env` 已填（DB / 告警 URL / STRICT）；dev 用 `.env.dev`（gitignore，archive 不传，需自建）
- [ ] `start_backend.sh` 有执行位（archive 可能丢 +x）
- [ ] `/health/status` 返回 `status: running`（非裸 `/health`，本地 curl）
- [ ] RTSP 网关在 venv 里起、**8004 和 18004 都在听**（端到端测试前置）
- [ ] e2e 前已与用户确认「目标 DB/告警端点可写测试数据」（有副作用）
- [ ] `test_single_client` 带 `CLEANSIGHT_ENV=<prod|dev>` 跑通（建任务→推流→`/api/start`→`/api/terminate`→自动清理）
- [ ] 部署窗口结束，源机分发服务已关
