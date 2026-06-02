#!/bin/bash
# 下载 MediaMTX 二进制,按系统自动选 Linux / Windows 版,放回对应目录。
# Windows 在 Git Bash 里跑本脚本即可。
#
# 为什么不进 git:第三方二进制 ~46MB,提交进去会让 .git 永久膨胀、clone 变慢。
# 仓库只留同目录的 mediamtx.yml / LICENSE(yml 已按本项目定制)。
#
# 内网不通外网:本地下好压缩包,scp 到机器,手动解压出二进制放进 mediamtx/ 即可,例如:
#   tar xzf mediamtx_v1.15.5_linux_amd64.tar.gz -C mediamtx mediamtx

set -e

DIR="mediamtx"   # 统一目录(含随 git 走的 mediamtx.yml / LICENSE)

case "$(uname -s)" in
    Linux*)
        VERSION="v1.15.5"
        ASSET="mediamtx_v1.15.5_linux_amd64.tar.gz"
        BIN="mediamtx"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        VERSION="v1.15.4"
        ASSET="mediamtx_v1.15.4_windows_amd64.zip"
        BIN="mediamtx.exe"
        ;;
    *)
        echo "不支持的系统:$(uname -s)" >&2; exit 1 ;;
esac

URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/${ASSET}"
cd "$(dirname "$0")/.."   # 切到项目根

if [ -x "${DIR}/${BIN}" ]; then
    echo "已存在 ${DIR}/${BIN},跳过。如需重装先删除它。"
    exit 0
fi

# 离线物料优先:vendor/mediamtx/ 里已备好就直接用,不联网(build_bundle.sh 的产物)
if [ -f "vendor/mediamtx/${ASSET}" ]; then
    SRC="vendor/mediamtx/${ASSET}"
    echo "用本地物料 ${SRC}(离线,跳过下载)"
else
    SRC="/tmp/${ASSET}"
    echo "下载 MediaMTX ${VERSION}(${ASSET})..."
    curl -fL "${URL}" -o "${SRC}"
fi

# 校验 SHA256:vendor/ 物料旁若有 build_bundle.sh 生成的 sidecar,强制比对
if [ -f "${SRC}.sha256" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
        echo "校验 SHA256..."
        echo "$(awk '{print $1}' "${SRC}.sha256")  ${SRC}" | sha256sum -c - \
            || { echo "SHA256 不匹配,物料可能损坏或被篡改" >&2; exit 1; }
        echo "SHA256 OK"
    else
        echo "无 sha256sum,跳过 SHA256 校验" >&2
    fi
fi

# 只取二进制,不覆盖项目里定制过的 mediamtx.yml / LICENSE
case "${ASSET}" in
    *.tar.gz)
        tar xzf "${SRC}" -C "${DIR}" "${BIN}"
        ;;
    *.zip)
        # Git Bash 默认不带 unzip,缺了就用 Python(项目本来就依赖)兜底
        if command -v unzip >/dev/null 2>&1; then
            unzip -o "${SRC}" "${BIN}" -d "${DIR}"
        else
            python -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract(sys.argv[2], sys.argv[3])" \
                "${SRC}" "${BIN}" "${DIR}"
        fi
        ;;
esac
chmod +x "${DIR}/${BIN}"
# 只删下载到 /tmp 的临时文件,别删 vendor/ 里的物料
[ "${SRC}" = "/tmp/${ASSET}" ] && rm -f "${SRC}"

echo "完成:$(${DIR}/${BIN} --version 2>&1 | head -1)"
