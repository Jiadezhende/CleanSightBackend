#!/bin/bash
# 构建静态部署物料：把核心重包 + 钉版二进制一次性下到本地，供 install.sh 离线消费。
#
# 产物（vendor/ 各持 linux+win 固定名两份，经源机 BASE_URL 同时服务两平台）：
#   wheelhouse/        torch/torchvision cu128 闭包（~6GB，含 nvidia CUDA；按 Python 3.10 打包，生产统一 3.10）+ SHA256SUMS
#   vendor/ffmpeg/     ffmpeg-linux-x64.tar.xz（钉版强校验）+ ffmpeg-win-x64.zip（钉版，开发机不钉 SHA）
#   vendor/mediamtx/   mediamtx-linux-x64.tar.gz（钉版强校验）+ mediamtx-win-x64.zip（开发机，不钉 SHA）
#
# 其余 Python 小包不入库，由 install.sh 在线拉。钉板版本/URL 在 deploy.conf；在线镜像写死本脚本。
# 逐物料幂等：已存在即跳过下载，可安全重跑、可复用已有 wheelhouse。
#
# 用法: ./build.sh   然后把 wheelhouse/ 和 vendor/ rsync 到目标机跑 ./install.sh

set -euo pipefail

cd "$(dirname "$0")"
source deploy.conf

# 在线镜像写死在脚本里（非「钉板物料」，不入 deploy.conf）。
TORCH_INDEX_URL="https://mirror.nju.edu.cn/pytorch/whl/cu128"

# ── 执行前环境检查 ──
[ "$(uname -s)" = "Linux" ]   || { echo "ERROR: 仅支持 Linux（当前 $(uname -s)）" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ]  || { echo "ERROR: 仅支持 x86_64（当前 $(uname -m)）" >&2; exit 1; }
for cmd in python3 pip curl sha256sum; do
    command -v "$cmd" >/dev/null || { echo "ERROR: 缺少 $cmd" >&2; exit 1; }
done
# 生产统一 Python 3.10：wheelhouse 按本机 Python 打 cp 标签，须用 3.10 构建，才能在生产（同样 3.10）离线安装。
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,10) else 1)' \
    || { echo "ERROR: 构建机须用 Python 3.10（与生产一致；当前 $(python3 -V)）" >&2; exit 1; }

mkdir -p wheelhouse vendor/ffmpeg vendor/mediamtx

# 断点续传 + 重试下载：下到 .part，仅在完整后落终名。
# 这样中断留下的半包不会被下方 [ -f ] 跳过逻辑误判为“已下好”。
# 网络抖动（GitHub 连接被 RST，curl exit 56）下可安全重跑，逐块续传。
fetch() {  # $1=url $2=终名
    local url="$1" dest="$2" part="${2}.part" tries=5 i
    for i in $(seq 1 "$tries"); do
        if curl -fL --progress-bar --retry 3 --retry-delay 2 --retry-all-errors \
                -C - -o "$part" "$url"; then
            mv -f "$part" "$dest"
            return 0
        fi
        echo "      下载中断（第 $i/$tries 次），3s 后续传…" >&2
        sleep 3
    done
    echo "ERROR: 多次重试后仍失败：$url" >&2
    return 1
}

# 校验/记录二进制 SHA256：conf 里钉了就强制比对，没钉就打印让人回填。
check_sha() {  # $1=文件 $2=deploy.conf 里的期望值
    local actual; actual="$(sha256sum "$1" | awk '{print $1}')"
    if [ -n "$2" ]; then
        [ "$actual" = "$2" ] || { echo "ERROR: SHA256 不匹配：$1" >&2; exit 1; }
        echo "      SHA256 OK"
    else
        echo "      SHA256 未钉，请回填 deploy.conf： $actual"
    fi
}

# ── [1] torch 闭包 → wheelhouse/（按 Python 3.10 打包；生产机统一用 3.10）──
echo "[1/3] torch 闭包 → wheelhouse/"
# wheelhouse 只为 Python 3.10 备 cp 专属 wheel（构建机已校验为 3.10）；生产机须统一用 3.10，
# 否则 install.sh 的 --no-index 会直接报 No matching distribution。开发机走在线、自动选版，不受此约束。
# 仅看 torch-*/torchvision-* 是否存在不足以判定可复用：上次 pip download 若在写完这两个
# 主包、还没下完 nvidia-cu* 等传递依赖时被中断，会留下「半包」。故用 --dry-run --ignore-installed
# 模拟目标机全新 venv 的离线解析：能解析才算完整闭包，可复用；否则重下，避免把半包封进 SHA256SUMS、
# 让 install.sh 的 --no-index 装到一半才报错。
if ls wheelhouse/torch-* >/dev/null 2>&1 && ls wheelhouse/torchvision-* >/dev/null 2>&1 \
   && pip install --no-index --find-links wheelhouse --dry-run --ignore-installed $TORCH_PKGS >/dev/null 2>&1; then
    echo "      已存在完整 torch 闭包，复用，跳过下载"
else
    echo "      下载 torch 闭包（缺料或闭包不完整时触发）..."
    pip download $TORCH_PKGS --index-url "$TORCH_INDEX_URL" -d wheelhouse
    # 下完即校：闭包必须能在全新环境离线解析，否则 install.sh 必然失败，早报早改。
    pip install --no-index --find-links wheelhouse --dry-run --ignore-installed $TORCH_PKGS >/dev/null \
        || { echo "ERROR: torch 闭包不完整，wheelhouse 缺少传递依赖" >&2; exit 1; }
fi
( cd wheelhouse && sha256sum *.whl > SHA256SUMS )
echo "      已写 wheelhouse/SHA256SUMS（$(wc -l < wheelhouse/SHA256SUMS) 个 wheel）"

# ── [2] ffmpeg → vendor/ffmpeg/（Linux 钉版强校验 + Windows 开发机包不钉 SHA）──
ff_asset="ffmpeg-linux-x64.tar.xz"
echo "[2/3] ffmpeg → vendor/ffmpeg/"
if [ -f "vendor/ffmpeg/${ff_asset}" ]; then
    echo "      ${ff_asset} 已存在，跳过下载"
else
    fetch "$FFMPEG_URL" "vendor/ffmpeg/${ff_asset}"
fi
check_sha "vendor/ffmpeg/${ff_asset}" "$FFMPEG_SHA256"
ff_win="ffmpeg-win-x64.zip"
if [ -f "vendor/ffmpeg/${ff_win}" ]; then
    echo "      ${ff_win} 已存在，跳过下载"
else
    fetch "$FFMPEG_WIN_URL" "vendor/ffmpeg/${ff_win}"   # 钉版，开发机不 check_sha
fi

# ── [3] mediamtx → vendor/mediamtx/（Linux 钉版强校验 + Windows 开发机包不钉 SHA）──
mtx_asset="mediamtx-linux-x64.tar.gz"
echo "[3/3] mediamtx → vendor/mediamtx/"
if [ -f "vendor/mediamtx/${mtx_asset}" ]; then
    echo "      ${mtx_asset} 已存在，跳过下载"
else
    fetch "$MEDIAMTX_URL" "vendor/mediamtx/${mtx_asset}"
fi
check_sha "vendor/mediamtx/${mtx_asset}" "$MEDIAMTX_SHA256"
mtx_win="mediamtx-win-x64.zip"
if [ -f "vendor/mediamtx/${mtx_win}" ]; then
    echo "      ${mtx_win} 已存在，跳过下载"
else
    fetch "$MEDIAMTX_WIN_URL" "vendor/mediamtx/${mtx_win}"   # 不 check_sha
fi

echo ""
echo "完成。物料目录："
du -sh wheelhouse vendor 2>/dev/null || true
echo "把 wheelhouse/ 和 vendor/ rsync 到目标机后跑 ./install.sh"
