#!/bin/bash
# 构建静态部署物料：把核心重包 + 钉版二进制一次性下到本地，供 install.sh 离线消费。
#
# 产物：
#   wheelhouse/        torch/torchvision cu128 闭包（~6GB，含 nvidia CUDA）+ SHA256SUMS
#   vendor/ffmpeg/     钉版 ffmpeg 静态包 + .sha256
#   vendor/mediamtx/   钉版 mediamtx 二进制包 + .sha256
#
# 其余 Python 小包不入库，由 install.sh 在线拉。版本/URL 全在 deploy.conf。
# 逐物料幂等：已存在即跳过下载，可安全重跑、可复用已有 wheelhouse。
#
# 用法: ./build.sh   然后把 wheelhouse/ 和 vendor/ rsync 到目标机跑 ./install.sh

set -euo pipefail

cd "$(dirname "$0")"
source deploy.conf

# ── 执行前环境检查 ──
[ "$(uname -s)" = "Linux" ]   || { echo "ERROR: 仅支持 Linux（当前 $(uname -s)）" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ]  || { echo "ERROR: 仅支持 x86_64（当前 $(uname -m)）" >&2; exit 1; }
for cmd in python3 pip curl sha256sum; do
    command -v "$cmd" >/dev/null || { echo "ERROR: 缺少 $cmd" >&2; exit 1; }
done

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

# ── [1] torch 闭包 → wheelhouse/ ──
echo "[1/3] torch 闭包 → wheelhouse/"
if ls wheelhouse/torch-* >/dev/null 2>&1 && ls wheelhouse/torchvision-* >/dev/null 2>&1; then
    echo "      已存在 torch wheel，复用，跳过下载"
else
    pip download $TORCH_PKGS --index-url "$TORCH_INDEX_URL" -d wheelhouse
fi
( cd wheelhouse && sha256sum *.whl > SHA256SUMS )
echo "      已写 wheelhouse/SHA256SUMS（$(wc -l < wheelhouse/SHA256SUMS) 个 wheel）"

# ── [2] ffmpeg → vendor/ffmpeg/ ──
ff_asset="$(basename "$FFMPEG_URL")"
echo "[2/3] ffmpeg → vendor/ffmpeg/${ff_asset}"
if [ -f "vendor/ffmpeg/${ff_asset}" ]; then
    echo "      已存在，跳过下载"
else
    fetch "$FFMPEG_URL" "vendor/ffmpeg/${ff_asset}"
fi
check_sha "vendor/ffmpeg/${ff_asset}" "$FFMPEG_SHA256"

# ── [3] mediamtx → vendor/mediamtx/ ──
mtx_asset="$(basename "$MEDIAMTX_URL")"
echo "[3/3] mediamtx → vendor/mediamtx/${mtx_asset}"
if [ -f "vendor/mediamtx/${mtx_asset}" ]; then
    echo "      已存在，跳过下载"
else
    fetch "$MEDIAMTX_URL" "vendor/mediamtx/${mtx_asset}"
fi
check_sha "vendor/mediamtx/${mtx_asset}" "$MEDIAMTX_SHA256"

echo ""
echo "完成。物料目录："
du -sh wheelhouse vendor 2>/dev/null || true
echo "把 wheelhouse/ 和 vendor/ rsync 到目标机后跑 ./install.sh"
