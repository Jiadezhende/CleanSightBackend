# Windows GPU 开发机一键安装（原生 PowerShell）
#
# 对标 install.sh，面向 Windows GPU 开发机：
#   torch/torchvision → 从 cu128 索引在线拉
#   其余 Python 依赖   → 从清华源在线拉
#   ffmpeg / mediamtx → BASE_URL 非空则从源机离线拉，空则从 deploy.conf 内在线 *_WIN_URL 拉；
#                       解压部署到项目内 .ffmpeg\ 与 mediamtx\。一处明确来源、无 PATH fallback、失败即报。
#
# 钉板版本/URL 在 deploy.conf（与 Linux 共享单一事实源，本脚本正则解析其中的 bash 变量）；
# 在线镜像写死本脚本。开发便利向：Windows 物料不做 SHA 强校验、不做服务化。生产请用 Linux build.sh + install.sh。
#
# 用法（在项目根目录）：
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\install.ps1                                        # 在线装（torch/ffmpeg/mediamtx 走公网）
#   $env:BASE_URL="http://<源机IP>:8080"; .\install.ps1  # 离线装（从源机 HTTP 拉）
# 装完启动：.\start_backend.ps1 dev

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Assert-LastExit($msg) {
    if ($LASTEXITCODE -ne 0) { Write-Error $msg }
}

# 下载并解压一个 zip 到临时目录，返回临时目录路径（调用方负责取用 + 清理）。
# 无 fallback：下载/解压失败时（$ErrorActionPreference=Stop）直接抛出中止。
function Expand-RemoteZip($url, $tag) {
    $zip = Join-Path $env:TEMP "cleansight-$tag.zip"
    $tmp = Join-Path $env:TEMP "cleansight-$tag-extract"
    $old = $ProgressPreference; $ProgressPreference = "SilentlyContinue"   # 大幅加速 Invoke-WebRequest
    try {
        Write-Host "      下载 $url ..."
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
    } finally {
        $ProgressPreference = $old
        if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
    }
    return $tmp
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
$TORCH_PKGS       = $conf["TORCH_PKGS"]
$FFMPEG_WIN_URL   = $conf["FFMPEG_WIN_URL"]
$MEDIAMTX_WIN_URL = $conf["MEDIAMTX_WIN_URL"]
foreach ($kv in @{ TORCH_PKGS = $TORCH_PKGS; FFMPEG_WIN_URL = $FFMPEG_WIN_URL; MEDIAMTX_WIN_URL = $MEDIAMTX_WIN_URL }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($kv.Value)) { Write-Error "deploy.conf 缺少 $($kv.Key)" }
}

# 在线镜像写死在脚本里（非「钉板物料」，不入 deploy.conf；与 install.sh 一致）。
$TORCH_INDEX_URL = "https://mirror.nju.edu.cn/pytorch/whl/cu128"
$PYPI_INDEX_URL  = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 源机物料基址：非空 → ffmpeg/mediamtx 从源机离线拉；空 → 用上面在线 *_WIN_URL。
# deploy.conf 里是 ${BASE_URL:-} 的 bash 占位、对 PS 无意义，故只认 env 或 conf 中已填的字面量。
$cb = $conf["BASE_URL"]; if ($cb -match '^\$\{') { $cb = '' }
$BASE_URL = if ($env:BASE_URL) { $env:BASE_URL } else { $cb }

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

# ── [1/3] Python 依赖：torch（cu128 在线）+ 其余（清华源）──
Write-Host "[1/3] Python 依赖" -ForegroundColor Green
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

# ── [2/3] ffmpeg → 项目内 .ffmpeg\（BASE_URL 离线优先，无 fallback，失败即报）──
# 必须钉版：ffmpeg 4.x/8.x 对 -hls_fmp4_init_filename 解析差异巨大，见 docs/HLS_TIMELINE_PITFALL.md。
Write-Host "[2/3] ffmpeg -> .ffmpeg\" -ForegroundColor Green
$ffUrl = if ($BASE_URL) { "$($BASE_URL.TrimEnd('/'))/vendor/ffmpeg/ffmpeg-win-x64.zip" } else { $FFMPEG_WIN_URL }
$ffTmp = Expand-RemoteZip $ffUrl "ffmpeg-win64"
try {
    $inner = Get-ChildItem -Path $ffTmp -Directory | Where-Object { $_.Name -like "ffmpeg-*" } | Select-Object -First 1
    if (-not $inner -or -not (Test-Path (Join-Path $inner.FullName "bin\ffmpeg.exe"))) {
        throw "ffmpeg 解压结构异常，找不到 bin\ffmpeg.exe（来源 $ffUrl）"
    }
    if (Test-Path ".\.ffmpeg") { Remove-Item ".\.ffmpeg" -Recurse -Force }
    Move-Item $inner.FullName ".\.ffmpeg"
    Write-Host "      ffmpeg 已部署到 .ffmpeg\bin\ffmpeg.exe（后端启动自动探测）"
} finally {
    if (Test-Path $ffTmp) { Remove-Item $ffTmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# ── [3/3] mediamtx → 项目内 mediamtx\（BASE_URL 离线优先，无 fallback；保留 git 跟踪的 mediamtx.yml/LICENSE）──
Write-Host "[3/3] mediamtx -> mediamtx\" -ForegroundColor Green
$mtxUrl = if ($BASE_URL) { "$($BASE_URL.TrimEnd('/'))/vendor/mediamtx/mediamtx-win-x64.zip" } else { $MEDIAMTX_WIN_URL }
$mtxTmp = Expand-RemoteZip $mtxUrl "mediamtx-win64"
try {
    # mediamtx win zip 为扁平结构（根含 mediamtx.exe / mediamtx.yml / LICENSE），只取 exe，不覆盖仓库 yml。
    $exe = Get-ChildItem -Path $mtxTmp -Recurse -Filter "mediamtx.exe" | Select-Object -First 1
    if (-not $exe) { throw "mediamtx 解压结构异常，找不到 mediamtx.exe（来源 $mtxUrl）" }
    New-Item -ItemType Directory -Force -Path ".\mediamtx" | Out-Null
    Copy-Item $exe.FullName ".\mediamtx\mediamtx.exe" -Force
    Write-Host "      mediamtx 已部署到 mediamtx\mediamtx.exe（同目录 mediamtx.yml 保留）"
} finally {
    if (Test-Path $mtxTmp) { Remove-Item $mtxTmp -Recurse -Force -ErrorAction SilentlyContinue }
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
& ".\.ffmpeg\bin\ffmpeg.exe" -version | Select-Object -First 1
& ".\mediamtx\mediamtx.exe" --version

Write-Host ""
Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor Green
Write-Host "安装完成。启动：.\start_backend.ps1 dev" -ForegroundColor Green
Write-Host "ffmpeg 已部署到 .ffmpeg\（后端自动探测）；mediamtx 已部署到 mediamtx\mediamtx.exe。" -ForegroundColor Green
Write-Host "提示：需要本地 PostgreSQL（在 .env.dev 配置连接）；RTSP 对外网关按需经 GATEWAY_MEDIAMTX_BIN 指向 mediamtx\mediamtx.exe。" -ForegroundColor Green
Write-Host "─────────────────────────────────────────────────────────────" -ForegroundColor Green
