#!/bin/bash
# 备料：把裸机离线部署需要的全部物料一次性下到本地，之后换机/重装 0 下载。
#
# 产物两个目录：
#   wheelhouse/        Python 依赖 wheel（torch cu128 走 NJU 镜像，其余走清华）
#   vendor/ffmpeg/     ffmpeg 钉版静态包（BtbN，URL 会被清理 → 必须囤）
#   vendor/mediamtx/   MediaMTX 二进制包（后端 prod 依赖，本机 127.0.0.1 直连）
#
# 用法: ./build.sh
# 之后把 wheelhouse/ 和 vendor/ rsync 到目标机，跑 ./install.sh 全离线安装。
#
# 提速：torch 全家桶 ~6GB，国内镜像约 1.2MB/s。临时把云带宽拉高 build 一次，再调回；
#       下载是入流量，几乎不计费。
# 双平台：本脚本备 Linux。Windows 侧另用 build.ps1（待补），合并 wheelhouse/ 与 vendor/。

set -e

WHEELHOUSE="wheelhouse"
# torch 镜像：南京大学（已实测可用），必须是 cu128 的 simple index
TORCH_INDEX="https://mirror.nju.edu.cn/pytorch/whl/cu128"
PYPI="https://pypi.tuna.tsinghua.edu.cn/simple"

mkdir -p "$WHEELHOUSE" vendor/ffmpeg vendor/mediamtx

# 跨平台算 sha256：Linux 用 sha256sum，mac 用 shasum；输出标准 "<hash>  <name>" 格式。
# 在物料所在目录内执行，sidecar 里存的是 basename 而非全路径，便于核对。
write_sha256() {  # $1 = 物料文件路径（相对/绝对均可）
    local dir base; dir="$(dirname "$1")"; base="$(basename "$1")"
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$dir" && sha256sum "$base" > "${base}.sha256" )
    else
        ( cd "$dir" && shasum -a 256 "$base" > "${base}.sha256" )
    fi
    echo "      sha256: $(awk '{print $1}' "$1.sha256")"
}

# ─────────────────────────────────────────────────────────────────────
# 1) Python 依赖 → wheelhouse/
#    分两步、错开索引：torch 闭包从 cu128 源（自洽，避开把纯包丢给 cu128 索引
#    导致的 MarkupSafe 解析死锁）；其余从清华，torch 用 --find-links 命中本地 wheel
#    不重复下载（防 ultralytics 的 torch 依赖把清华那份捆绑 CUDA 的 torch 拉回来）。
# ─────────────────────────────────────────────────────────────────────
echo "[1/3] Python 依赖 → ${WHEELHOUSE}/"
echo "      - torch cu128 闭包（${TORCH_INDEX}）"
pip download torch==2.8.0 torchvision==0.23.0 --index-url "$TORCH_INDEX" -d "$WHEELHOUSE"
echo "      - 其余依赖（${PYPI}）"
REQ_NO_TORCH="$(mktemp)"
grep -vE '^[[:space:]]*(torch|torchvision)[[:space:]]*==' requirements.txt > "$REQ_NO_TORCH"
pip download -r "$REQ_NO_TORCH" --find-links "$WHEELHOUSE" --index-url "$PYPI" -d "$WHEELHOUSE"
rm -f "$REQ_NO_TORCH"

# ─────────────────────────────────────────────────────────────────────
# 2) ffmpeg → vendor/ffmpeg/（版本号取自 scripts/install_ffmpeg.sh，单一真源不漂移）
# ─────────────────────────────────────────────────────────────────────
FF_TAG=$(grep -E '^FFMPEG_BUILD_TAG=' scripts/install_ffmpeg.sh | cut -d'"' -f2)
FF_ASSET=$(grep -E '^FFMPEG_ASSET=' scripts/install_ffmpeg.sh | cut -d'"' -f2)
[ -n "$FF_ASSET" ] || { echo "ERROR: 没从 scripts/install_ffmpeg.sh 解析出 FFMPEG_ASSET" >&2; exit 1; }
echo "[2/3] ffmpeg ${FF_TAG} → vendor/ffmpeg/${FF_ASSET}"
if [ -f "vendor/ffmpeg/${FF_ASSET}" ]; then
    echo "      已存在，跳过"
else
    curl -fL --progress-bar -o "vendor/ffmpeg/${FF_ASSET}" \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FF_TAG}/${FF_ASSET}"
fi
write_sha256 "vendor/ffmpeg/${FF_ASSET}"   # install_ffmpeg.sh 离线安装时强制比对此 sidecar

# ─────────────────────────────────────────────────────────────────────
# 3) mediamtx → vendor/mediamtx/（版本号取自 scripts/install_mediamtx.sh）
# ─────────────────────────────────────────────────────────────────────
MTX_ASSET=$(grep -oE 'mediamtx_v[0-9.]+_linux_amd64\.tar\.gz' scripts/install_mediamtx.sh | head -1)
MTX_VER=$(echo "$MTX_ASSET" | grep -oE 'v[0-9.]+' | head -1)
[ -n "$MTX_ASSET" ] || { echo "ERROR: 没从 scripts/install_mediamtx.sh 解析出 mediamtx 资产名" >&2; exit 1; }
echo "[3/3] mediamtx ${MTX_VER} → vendor/mediamtx/${MTX_ASSET}"
if [ -f "vendor/mediamtx/${MTX_ASSET}" ]; then
    echo "      已存在，跳过"
else
    curl -fL -o "vendor/mediamtx/${MTX_ASSET}" \
        "https://github.com/bluenviron/mediamtx/releases/download/${MTX_VER}/${MTX_ASSET}"
fi
write_sha256 "vendor/mediamtx/${MTX_ASSET}"   # install_mediamtx.sh 离线安装时强制比对此 sidecar

echo ""
echo "完成。物料目录："
du -sh "$WHEELHOUSE" vendor 2>/dev/null || true
echo "把 ${WHEELHOUSE}/ 和 vendor/ rsync 到目标机后跑 ./install.sh"
