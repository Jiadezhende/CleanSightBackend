# 第三方二进制物料

FFmpeg 是社区快照构建，上游链接可能过期，各平台构建分别验证。MediaMTX 使用固定发行版，两者统一归档到源机，安装从该归档获取。

## 已归档路径

源机地址由用户在 `/install` 请求中提供；当前任务中已提供则复用，下载前缺失则询问，不预设主机或端口。路径相对于分发根目录：

```text
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
vendor/ffmpeg/ffmpeg-win-x64.zip
vendor/mediamtx/mediamtx-linux-x64.tar.gz
vendor/mediamtx/mediamtx-win-x64.zip
```

源机已有归档应复用，无需每次重新运行构建。源机是否开启由用户确认；需要远程启动分发服务时，先确认目标、访问方式及操作授权，由管理员按该源机的管理方式处理。

## 更新物料

根目录 `build.sh` 保存各平台上游 URL，当前 FFmpeg 为 n7.1.4 系列构建、MediaMTX 为 v1.15.5。脚本仅下载归档，不构建 Python 环境。运行需 Bash、curl 和足够磁盘空间。

```bash
bash build.sh
```

已存在的归档跳过，断点保存在 `.part`。上游快照失效时获取对应平台兼容构建并更新 URL；删除待替换归档前保留旧包用于回退。固定归档名仅表示安装接口，具体构建版本以维护者已验证内容为准。

在对应平台用安装工具运行二进制版本命令并按需要验证编解码后，再发布到源机。新归档先上传临时文件，完成后再替换分发文件，避免安装者下载半包。Python 包不再通过源机分发；旧 wheelhouse 不再使用，本轮不删除远程物料。

## 安装与结果

在项目根目录，以项目虚拟环境 Python 执行：

```bash
python .claude/skills/install/scripts/install_binaries.py all --base-url "<源机地址>"
```

执行前将占位符替换为用户提供的 URL。FFmpeg 写入 `.ffmpeg/`，MediaMTX 写入 `.mediamtx/`。新包检查成功前保留旧安装。可分别指定 `ffmpeg` 或 `mediamtx`，适用于只补缺项或失败后重试。

只有安装目录中可用的二进制才可复用；从旧 `mediamtx/mediamtx(.exe)` 布局升级时，重新安装 MediaMTX。受版本控制的配置位于 `config/mediamtx.yml`，原 `mediamtx/LICENSE` 继续保留。

安装与版本自检不需要应用环境文件和服务端口。源机只暴露物料目录，不暴露源码、运行配置或密钥。
