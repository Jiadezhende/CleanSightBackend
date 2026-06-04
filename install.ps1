# Windows GPU 开发机一键安装（原生 PowerShell，在线安装）
#
# 对标 install.sh，但面向 Windows GPU 开发机、走在线安装：
#   torch/torchvision → 从 cu128 索引在线拉（不依赖 build.sh 的 wheelhouse 离线物料）
#   其余 Python 依赖   → 从清华源在线拉
#   ffmpeg            → 在线下 BtbN win64 静态包，解到项目内 .ffmpeg\（失败回退 PATH）
#   mediamtx          → 开发不需要（RTSP 对外网关），略；需要时手动装
#
# 物料版本/URL 全在 deploy.conf（与 Linux 共享单一事实源，本脚本正则解析其中的 bash 变量）。
# 开发便利向：不做离线物料 SHA 强校验、不做服务化。生产请用 Linux build.sh + install.sh。
#
# 用法（在项目根目录）：
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\install.ps1
# 装完启动：.\start_backend.ps1 dev

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Assert-LastExit($msg) {
    if ($LASTEXITCODE -ne 0) { Write-Error $msg }
}

# ── 解析 deploy.conf（纯 bash 变量：KEY="value"）──
function Read-DeployConf {
    $conf = @{}
    foreach ($line in Get-Content "deploy.conf") {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"?([^"]*)"?\s*$') {
            $conf[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $conf
}
$conf = Read-DeployConf
$TORCH_INDEX_URL   = $conf["TORCH_INDEX_URL"]
$TORCH_PKGS        = $conf["TORCH_PKGS"]
$PYPI_INDEX_URL    = $conf["PYPI_INDEX_URL"]
$FFMPEG_WIN_URL    = $conf["FFMPEG_WIN_URL"]
# 可选：指向离线分发源（留空 = 从 FFMPEG_WIN_URL 在线拉；填 = 从该目录基址拉，与 Linux FFMPEG_SRC_URL 同规则）
$FFMPEG_WIN_SRC_URL = if ($env:FFMPEG_WIN_SRC_URL) { $env:FFMPEG_WIN_SRC_URL } else { $conf["FFMPEG_WIN_SRC_URL"] }
foreach ($kv in @{ TORCH_INDEX_URL = $TORCH_INDEX_URL; TORCH_PKGS = $TORCH_PKGS; PYPI_INDEX_URL = $PYPI_INDEX_URL; FFMPEG_WIN_URL = $FFMPEG_WIN_URL }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($kv.Value)) { Write-Error "deploy.conf 缺少 $($kv.Key)" }
}

# ── 执行前环境检查 ──
if ($env:OS -ne "Windows_NT") { Write-Error "仅支持 Windows（Linux 请用 ./install.sh）" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "缺少 python（需 3.10–3.13，并加入 PATH）"
}
& python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)"
if ($LASTEXITCODE -ne 0) {
    $pv = (& python -V) 2>&1
    Write-Error "需要 Python 3.10–3.13（当前 $pv）"
}
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host "检测到 GPU：" -ForegroundColor Cyan
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
} else {
    Write-Warning "未检测到 nvidia-smi —— 本脚本面向 GPU 开发机，无 GPU 时末尾 torch.cuda 自检会失败。"
}

# ── 虚拟环境 ──
if (-not (Test-Path ".\.venv\Scripts\Activate.ps1")) {
    Write-Host "创建虚拟环境 .venv ..."
    & python -m venv .venv
    Assert-LastExit "创建虚拟环境失败"
}
& ".\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
Assert-LastExit "升级 pip 失败"

# ── [1/2] Python 依赖：torch（cu128 在线）+ 其余（清华源）──
Write-Host "[1/2] Python 依赖" -ForegroundColor Green
Write-Host "      torch 闭包（cu128 在线，$TORCH_INDEX_URL）..."
pip install ($TORCH_PKGS -split '\s+') --index-url $TORCH_INDEX_URL
Assert-LastExit "torch 安装失败（若 cu128 索引无 win_amd64 wheel，可改用官方源 https://download.pytorch.org/whl/cu128）"
Write-Host "      其余依赖（在线，$PYPI_INDEX_URL）..."
pip install -r requirements.txt -i $PYPI_INDEX_URL
Assert-LastExit "requirements.txt 安装失败"

# ultralytics 会拉入 opencv-python，与 headless 版共享 cv2/ 文件。force-reinstall 默认连依赖
# 一起重装会把 numpy 顶到 2.x（撞 torch ABI），故 --no-deps 只重铺 cv2、不碰 numpy；
# 随后显式复位 numpy 以防已被顶。（逻辑与 install.sh 一致）
Write-Host "      修复 opencv headless..."
pip uninstall -y opencv-python opencv-python-headless 2>$null
pip install -i $PYPI_INDEX_URL --no-deps --force-reinstall "opencv-python-headless<4.12.0"
Assert-LastExit "opencv-python-headless 安装失败"
pip install -i $PYPI_INDEX_URL "numpy==1.26.4"
Assert-LastExit "numpy 复位失败"

# ── [2/2] ffmpeg → 项目内 .ffmpeg\（离线源 > 在线，失败回退 PATH）──
# 来源规则：FFMPEG_WIN_SRC_URL 非空 → 从该目录基址拼接文件名拉取（离线分发源）；空 → 从 FFMPEG_WIN_URL 在线拉。
Write-Host "[2/2] ffmpeg -> .ffmpeg\" -ForegroundColor Green
$ffAssetName = "ffmpeg-win-x64.zip"
$ffResolvedUrl = if ($FFMPEG_WIN_SRC_URL) {
    "$($FFMPEG_WIN_SRC_URL.TrimEnd('/'))/$ffAssetName"
} else {
    $FFMPEG_WIN_URL
}
$ffZip = Join-Path $env:TEMP "cleansight-ffmpeg-win64.zip"
$ffTmp = Join-Path $env:TEMP "cleansight-ffmpeg-extract"
$ffOk = $false
$oldProgress = $ProgressPreference
$ProgressPreference = "SilentlyContinue"   # 大幅加速 Invoke-WebRequest
try {
    Write-Host "      下载 $ffResolvedUrl ..."
    Invoke-WebRequest -Uri $ffResolvedUrl -OutFile $ffZip -UseBasicParsing
    if (Test-Path $ffTmp) { Remove-Item $ffTmp -Recurse -Force }
    Expand-Archive -Path $ffZip -DestinationPath $ffTmp -Force
    $inner = Get-ChildItem -Path $ffTmp -Directory | Where-Object { $_.Name -like "ffmpeg-*" } | Select-Object -First 1
    if (-not $inner -or -not (Test-Path (Join-Path $inner.FullName "bin\ffmpeg.exe"))) {
        throw "解压结构异常，找不到 bin\ffmpeg.exe"
    }
    if (Test-Path ".\.ffmpeg") { Remove-Item ".\.ffmpeg" -Recurse -Force }
    Move-Item $inner.FullName ".\.ffmpeg"
    $ffOk = $true
    Write-Host "      ffmpeg 已部署到 .ffmpeg\bin\ffmpeg.exe（后端启动自动探测）"
} catch {
    Write-Warning "ffmpeg 下载/解压失败：$($_.Exception.Message)"
    if ($FFMPEG_WIN_SRC_URL) {
        Write-Warning "离线源 $FFMPEG_WIN_SRC_URL 不通。确认源机 cleansight-dist 已按部署窗口 start。"
    } else {
        Write-Warning "URL 可能已失效。可配置 `$env:FFMPEG_WIN_SRC_URL=http://<源机IP>:8080/vendor/ffmpeg/ 改用离线源。"
    }
    Write-Warning "回退方案：把 ffmpeg.exe 放进 PATH（或 'choco install ffmpeg'），后端会自动探测，无需改 .env。"
} finally {
    $ProgressPreference = $oldProgress
    if (Test-Path $ffZip) { Remove-Item $ffZip -Force -ErrorAction SilentlyContinue }
    if (Test-Path $ffTmp) { Remove-Item $ffTmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# ── .env.dev 便利生成（首次安装）──
if (-not (Test-Path ".\.env.dev") -and (Test-Path ".\.env.example")) {
    Copy-Item ".\.env.example" ".\.env.dev"
    Write-Host "已从 .env.example 生成 .env.dev —— 请填写数据库等配置后再启动。" -ForegroundColor Yellow
}

# ── 执行后验证 ──
# 重点验冲突面：import ultralytics 会连带 import cv2，是 opencv 非 headless 与 headless 共存、
# 以及 numpy 被顶到 2.x（撞 torch ABI）最易暴露的入口。GPU 开发机此处强校验 CUDA。
Write-Host ""
Write-Host "验证安装..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path ".ultralytics" | Out-Null
$env:YOLO_CONFIG_DIR = (Resolve-Path ".ultralytics").Path
$verify = @'
import torch, numpy, cv2, ultralytics
from ultralytics import YOLO  # 触发 ultralytics 完整导入图（含 cv2）
assert torch.cuda.is_available(), "CUDA 不可用"
assert numpy.__version__.startswith("1.26"), f"numpy 被顶到 {numpy.__version__}（撞 torch ABI）"
print(f"torch {torch.__version__} | numpy {numpy.__version__} | cv2 {cv2.__version__} "
      f"| ultralytics {ultralytics.__version__} | CUDA OK")
'@
python -c $verify
Assert-LastExit "Python 依赖自检失败"
if ($ffOk) { & ".\.ffmpeg\bin\ffmpeg.exe" -version | Select-Object -First 1 }

Write-Host ""
Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor Green
Write-Host "安装完成。启动：.\start_backend.ps1 dev" -ForegroundColor Green
if ($ffOk) { Write-Host "ffmpeg 已部署到项目内 .ffmpeg\，后端自动探测，无需配置 .env。" -ForegroundColor Green }
Write-Host "提示：需要本地 PostgreSQL（在 .env.dev 配置连接）；RTSP 对外网关 mediamtx 开发可省。" -ForegroundColor Green
Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor Green
