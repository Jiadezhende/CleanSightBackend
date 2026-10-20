# Windows + NVIDIA：install.ps1 路径（开发机）

仅为开发便利，不作为生产标准，不做服务化。角色通常是 dev；test 也能跑（端口 +2）。流程同 SKILL.md 主流程：装依赖 → 配 `.env*` → 启动 → 权重核对。

## 固定约束

| 项 | 值 |
|----|----|
| Python | 3.10–3.13，`python` 在 PATH（torch 在线按本机版本自动选 wheel，不受 3.10 约束） |
| GPU | NVIDIA 驱动就位，`nvidia-smi` 可用；安装末尾强校验 `torch.cuda.is_available()` |
| 网络 | 能访问源机 `BASE_URL`、清华 PyPI 镜像、南大 cu128 镜像 |
| 其他 | 本地 PostgreSQL（或指向别处的库） |

## 安装

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1                                        # 源机地址已写死在脚本开头
$env:BASE_URL="<用户给的源机地址>"; .\install.ps1    # 用户给了 BASE_URL 就这样传
```

与 Linux 的差别只在 torch 从哪来：wheelhouse 是 Linux cp310 轮子，Windows 用不了，改从 cu128 索引在线拉，版本与索引钉在 `requirements/gpu.txt`（带 `+cu128` 后缀是刻意的，防止从清华主索引静默装成 CPU 构建）。ffmpeg 与 MediaMTX 同样取自源机 `vendor/`，与 Linux 一致。

脚本依次做：建 `.venv` → `pip install -r requirements/gpu.txt` → 修 opencv-headless 并钉 numpy 1.26.4 → ffmpeg 到 `.ffmpeg\` → mediamtx.exe 到 `mediamtx\` → 首次安装从 `.env.example` 生成 `.env.dev` → 自检（torch / CUDA / cv2 / ultralytics / ffmpeg / mediamtx）。

安装产物：

```text
.venv\
.ffmpeg\bin\ffmpeg.exe
mediamtx\mediamtx.exe      # 同目录 mediamtx.yml 随仓库走，不覆盖
.env.dev                   # 首次生成，要填 DB 等再启动
```

## 配 .env.dev

`install.ps1` 首次会从 `.env.example` 复制出 `.env.dev`，之后按 [runtime-config.md](runtime-config.md) 三步走：按场景写非敏感项（本机开发：`DEBUG=true`、`LOG_LEVEL=DEBUG`、`STRICT=0`、`GATEWAY_ENABLED=false`，告警占位符换成 `alarm.invalid`）→ 交接给用户填 DB 五项 → 键检查。命令在 Claude Code 的 Git Bash 里照跑，不读值。

## 启动

```powershell
.\start_backend.ps1 dev
```

一条命令拉起网关（含 MediaMTX）+ 后端，dev 带 `--reload`。脚本启动前会查 8004 / 18004 占用，被占就列出占用进程并拒绝启动，不代为杀进程。验证：`http://localhost:8000/health/status` 返回 200（不是裸 `/health`）。后端输出只按关键字筛（同 linux.md [7]），不贴原文。

启动通过后按 runtime-config.md「模型权重」核对，缺的请用户从魔搭补齐。

## 已知坑

### 坑 1：18004 绑定失败，`netstat` 却看不到占用者

装了 Docker Desktop / WSL2 的机器，HNS 会随机预留一整块端口（曾见 `17320–20000`），对 `netstat` 与 `netsh excludedportrange` 都不可见，但 bind 直接报 `10048`。块在重启后漂移。永久修法（管理员 PowerShell）：

```powershell
net stop winnat
netsh int ipv4 add excludedportrange protocol=tcp startport=18004 numberofports=1 store=persistent
netsh int ipv4 add excludedportrange protocol=tcp startport=18006 numberofports=1 store=persistent   # test 角色用
net start winnat
```

显式排除只挡自动分配器，应用仍能正常 bind。诊断时用 `.venv\Scripts\python.exe` 循环 bind 一段端口打印失败区间，比 netstat 可靠。

### 坑 2：上次 Ctrl+C 后遗留孤儿 `mediamtx.exe`

`start_backend.ps1` 退出时用 `taskkill /T` 连子进程一起清，但被强杀时可能留下孤儿占住 18004。启动脚本会报出占用 PID，手动 `taskkill /T /F /PID <PID>`。

### 坑 3：跑完 test 再手动 `python -m app.main`，端口不对

`.ps1` 在调用方会话内运行，脚本已在退出时还原端口环境变量；若是旧版本脚本或异常退出，检查 `$env:CLEANSIGHT_PORT` 是否残留。

## 部署完成检查清单

- [ ] `install.ps1` 末尾自检全过（含 `CUDA OK`）
- [ ] `.env.dev` 非敏感项按场景写好，键检查全「已填」，告警端点指向不可达地址；全程未读值
- [ ] `start_backend.ps1 dev` 起来，`/health/status` 返回 `running`
- [ ] 8004 与 18004 都在听
- [ ] 权重核对无缺失（或缺失清单已交给用户）
