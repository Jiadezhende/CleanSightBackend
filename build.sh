#!/bin/bash
# 准备 Windows / Linux 第三方二进制归档，上传源机后供 deploy skill 使用。
# FFmpeg 上游快照可能过期；复用已验证的源机归档，更新时分别确认各平台构建。
set -euo pipefail
cd "$(dirname "$0")"

FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-06-01-15-02/ffmpeg-n7.1.4-7-gadcf20da26-linux64-gpl-7.1.tar.xz"
FFMPEG_WIN_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-06-01-15-02/ffmpeg-n7.1.4-7-gadcf20da26-win64-gpl-7.1.zip"

# MediaMTX 固定发行归档。
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/v1.15.5/mediamtx_v1.15.5_linux_amd64.tar.gz"
MEDIAMTX_WIN_URL="https://github.com/bluenviron/mediamtx/releases/download/v1.15.5/mediamtx_v1.15.5_windows_amd64.zip"


for cmd in curl; do
    command -v "$cmd" >/dev/null || { echo "ERROR: 缺少 $cmd" >&2; exit 1; }
done
mkdir -p vendor/ffmpeg vendor/mediamtx

# 中断下载保存在 .part，完成后重命名，可重试和续传。
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

# ── [1] ffmpeg → vendor/ffmpeg/（linux + win 两份固定名，供源机同时服务两平台）──
ff_asset="ffmpeg-linux-x64.tar.xz"
echo "[1/2] ffmpeg → vendor/ffmpeg/"
if [ -f "vendor/ffmpeg/${ff_asset}" ]; then
    echo "      ${ff_asset} 已存在，跳过下载"
else
    fetch "$FFMPEG_URL" "vendor/ffmpeg/${ff_asset}"
fi
ff_win="ffmpeg-win-x64.zip"
if [ -f "vendor/ffmpeg/${ff_win}" ]; then
    echo "      ${ff_win} 已存在，跳过下载"
else
    fetch "$FFMPEG_WIN_URL" "vendor/ffmpeg/${ff_win}"
fi

# ── [2] mediamtx → vendor/mediamtx/（linux + win 两份固定名）──
mtx_asset="mediamtx-linux-x64.tar.gz"
echo "[2/2] mediamtx → vendor/mediamtx/"
if [ -f "vendor/mediamtx/${mtx_asset}" ]; then
    echo "      ${mtx_asset} 已存在，跳过下载"
else
    fetch "$MEDIAMTX_URL" "vendor/mediamtx/${mtx_asset}"
fi
mtx_win="mediamtx-win-x64.zip"
if [ -f "vendor/mediamtx/${mtx_win}" ]; then
    echo "      ${mtx_win} 已存在，跳过下载"
else
    fetch "$MEDIAMTX_WIN_URL" "vendor/mediamtx/${mtx_win}"
fi

echo "完成。将 vendor/ 同步到源机分发目录。"
