# Windows 启动脚本：退出杀整棵进程树 + 启动前端口占用自检

> **变更状态**：生效中（2026-09-04）
> **知识库**：待沉淀

## 概述

修 Windows 开发机上 `mediamtx.exe` 变孤儿、占住 18004 导致下次启动失败的问题。仅改 [start_backend.ps1](../../start_backend.ps1)：退出清理由 `Stop-Process -Force` 换成 `taskkill /T /F`（杀整棵子树），并在启动前对 `$ProxyPort` / `$InternalPort` 做占用自检、有占用则拒绝启动。不影响 Linux 侧和任何应用代码。

## 变更背景

- **现状 / 痛点**：`.ps1` 起的进程树是 `powershell → python -m mediamtx_gateway.main → mediamtx.exe`。旧的 `finally` 用 `Stop-Process -Id $gw.Id -Force`，只杀中间那层 python，孙进程 `mediamtx.exe` 留成孤儿继续监听 18004。下次启动 MediaMTX bind 失败，网关进重启循环，最终暴露成一堆「拉不到流」的下游报错，排查成本远高于成因。
- **触发来源**：Windows 开发机反复出现「上一次 Ctrl+C 之后就起不来了」。
- **为什么 `.sh` 没这个问题**：Linux 侧靠两个 Windows 不存在的机制兜底——
  1. `trap 'kill $GW_PID' EXIT INT TERM` 发的是 **SIGTERM**，是协作式的，网关能跑完自己的退出逻辑（[mediamtx_gateway/main.py](../../mediamtx_gateway/main.py) 里注册 SIGINT/SIGTERM → 置 `stop_event` → `proc.terminate()`，5s 不退再 `proc.kill()`），mediamtx 由网关亲手收掉；
  2. Ctrl+C 时 tty 把 SIGINT 发给**整个前台进程组**，mediamtx 自己也直接收到。

  Windows 上 `Stop-Process -Force` 等于 `TerminateProcess()`，内核直接抹掉进程，不给 handler 机会——网关那段 `proc.terminate()` 清理代码在强杀路径下**从未执行过**；且 Windows 默认没有父子进程的连带终止语义（未使用 Job Object），孙进程天然自由。

> 同一症状（18004 起不来）在本机有过两个不同成因，别混淆：
> - `Get-NetTCPConnection -LocalPort 18004` **有** owner PID → 本文这类，孤儿进程占用；
> - **没有** owner 却 bind 失败 → Docker/WSL 的 HNS 保留端口块抢占，需 `netsh int ipv4 add excludedportrange` 持久排除。

## 方案详情

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A（采用）退出用 `taskkill /T /F` 杀子树 + 启动前只检测不杀 | 仅动启动脚本；强杀范围限于本脚本自己拉起的子树 | 选它。根治孤儿，且不替人决定别人的进程该不该死 |
| B 退出前让网关走优雅路径（`CTRL_BREAK_EVENT`） | 需把网关放进独立 process group，改 `Start-Process` 起法甚至加 C# 互操作 | 否。代价远超收益，Windows 下没有便宜的优雅退出路径 |
| C 启动前自动 `taskkill` 掉占端口的进程 | 实现最省事 | 否。占用者可能是无关服务或人在调的另一个实例，脚本没资格代为强杀 |

### 1. `start_backend.ps1` — 退出时杀整棵进程树

#### 旧
```powershell
# 后端退出时一并清理网关及其子进程
if ($gw -and -not $gw.HasExited) { Stop-Process -Id $gw.Id -Force -ErrorAction SilentlyContinue }
```

#### 新
```powershell
# 后端退出时一并清理网关及其子进程（MediaMTX）。
# 必须用 taskkill /T：Stop-Process -Force 只杀网关 python，会把 mediamtx.exe 留成孤儿占住 18004。
if ($gw) { taskkill /T /F /PID $gw.Id 2>$null | Out-Null }
```

> 这里强杀是**刻意保留**的：杀的是本脚本自己 `Start-Process` 起来的子树，不涉及外部进程；旧实现本就是 `-Force`，本次只是从「杀一层」改成「杀整棵」。

### 2. `start_backend.ps1` — 启动前端口占用自检（只报告，不杀）

新增于日志目录创建之后、`Start-Process` 拉起网关之前。检测 `$ProxyPort` / `$InternalPort` 的 Listen 占用，解析出**进程名 + PID + 启动时间**（启动时间是判断「是否上一轮遗留」最直接的证据），打印人工排查/清理命令后 `exit 1`。

```powershell
$owners = (Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue).OwningProcess
...
    try { $desc += ", started $($proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))" } catch {}
...
if ($conflicts.Count -gt 0) { ...; exit 1 }
```

> - **fail fast 而非警告后继续**：端口被占则 MediaMTX 必然 bind 失败，继续跑只会把错误推迟到更难懂的地方。
> - **按端口而非进程名定位**：跑在其他端口的实例（如 test 环境 +100）不会被误报。
> - `$proc.StartTime` 对高权限进程会抛访问拒绝，取不到就降级只显示 PID。

### 3. 保留项（不改动）

- [mediamtx_gateway/main.py](../../mediamtx_gateway/main.py) 的信号处理与 `_run_mediamtx` 退出逻辑不动——它在 Linux 上是有效的主清理路径，Windows 只是收不到信号而已。
- `start_backend.sh` 不动，见下方遗留风险。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 正常退出（后端结束 / Ctrl+C 走到 finally） | mediamtx.exe 残留占 18004 | 整棵子树清干净 |
| 上轮被强杀后再启动 | 静默继续 → MediaMTX bind 失败 → 网关重启循环 → 报「拉不到流」 | 启动前直接拦下，打印占用者进程名/PID/启动时间与清理命令 |
| 端口上是无关服务 | （旧提交的中间版本会直接杀掉） | 只报告，由人决定 |

**自测结果**

| 项 | 结果 |
|----|------|
| PowerShell 语法校验（`Parser::ParseFile`） | parse OK, no errors |
| 实跑启动 | 未执行（会拉起真实网关 + MediaMTX） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `start_backend.sh` 无对应的启动前端口自检 | 网关被 `SIGKILL` / OOM killer 干掉或掉电时，Linux 同样会留下孤儿 mediamtx 被 init 收养占住 18004 | 低优先。日常 Ctrl+C 走的是有 trap 的路径所以从未暴露；要对齐可补一段 `ss -lptn "sport = :$INTERNAL_PORT"` 检测 + 拒绝启动（同样不自动杀） |
| 自检只覆盖 TCP 的 `$ProxyPort` / `$InternalPort` | RTP/RTCP 的 UDP 端口未检测 | 不补。孤儿 mediamtx 必然同时占住 18004，TCP 检测已足够识别 |
| 新增的 fail-fast 分支未实跑验证 | 端口冲突时的输出格式可能有瑕疵 | 下次真遇到冲突时顺手确认 |
