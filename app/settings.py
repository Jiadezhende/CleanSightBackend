import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_env_files():
    """根据 CLEANSIGHT_ENV 环境变量加载对应的配置文件

    - CLEANSIGHT_ENV=dev  → 加载 .env.dev（默认）
    - CLEANSIGHT_ENV=test → 加载 .env.test
    - CLEANSIGHT_ENV=prod → 加载 .env

    该函数会把键值对注入到 `os.environ`，以便 Pydantic 从环境读取。
    """
    base = Path(__file__).parent.parent
    env = os.environ.get("CLEANSIGHT_ENV", "dev").lower()

    # 根据环境变量确定配置文件
    env_files = {"dev": ".env.dev", "test": ".env.test", "prod": ".env"}

    env_file_name = env_files.get(env, ".env.dev")
    env_path = base / env_file_name

    # 记录是否为开发模式（供后续校验逻辑判断）
    global _LOADED_DEV
    _LOADED_DEV = env == "dev"

    # 加载环境文件
    if not env_path.exists():
        print(f"[Settings] Warning: Environment file '{env_file_name}' not found")
        return

    candidates = [env_path]
    for p in candidates:
        try:
            p = Path(p)
            if not p.exists():
                continue
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ[k] = v
        except Exception:
            # 不要在导入阶段让 .env 文件加载失败阻塞应用
            continue


class Settings(BaseSettings):
    # 数据库配置 - 无默认值，必须从环境变量读取
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # 应用配置
    debug: bool = False
    strict: bool = False

    # 外部接口URL（必需配置）
    alarm_report_url: str

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # 日志配置
    log_level: str = "INFO"
    log_config: str = "logging_config.json"

    # 外部工具（ffmpeg_path 留空 = 用项目自包含的 .ffmpeg/bin/ffmpeg，不回退 PATH）
    ffmpeg_path: str = ""

    # 模型路径
    model_path: str = "./app/data"

    # 持久化存储根目录（单一真源）。env: CLEANSIGHT_STORAGE_DIR
    # persistence / inference / traceback 三方都读 settings.storage_base_dir，
    # 不再各自重算或互相 push（消除跨服务穿透）。
    storage_dir: str = "./database"

    # 视频/推理帧率与队列（跨模块单一真源；inference / stream / client / persistence 四方共读，
    # 不再寄生在 inference_config.yaml 的 global 块里互相反向依赖）。env: CLEANSIGHT_RAW_FPS 等。
    raw_fps: int = 30          # 生产者源：解码 CFR 帧率（decoder default_fps、HLS raw fallback、CA 秒→帧数换算全派生自此）
    inference_decimation: int = 2  # 采样器：检测抽帧降采样倍率——系统唯一采样旋钮。抽帧器「每 N 帧留 1」直接用它。
                               # 检测率 = raw_fps / N（N=2→15fps，派生见 inference_fps property）。整数因子故只能命中
                               # raw_fps 的整除率（30→15/10/7.5/6…，不支持 30→20 类非整除比）；模型侧另按 ts 重采样到 7.5。
                               # env: CLEANSIGHT_INFERENCE_DECIMATION
    # CA 缓存/段长本是"时间概念"，以秒声明（时间为跨子系统货币）；帧数在各消费边界按 raw_fps 显式换算。
    ca_maxlen_seconds: int = 90    # CA 队列缓存时长（秒）→ 帧数 = ×raw_fps
    ca_segment_seconds: int = 10   # HLS 段时长（秒）→ 帧数 = ×raw_fps

    # MediaMTX 端口映射（内部拉流时绕过 RTSPProxy 直连 MediaMTX）
    mediamtx_proxy_port: int = 8004      # RTSPProxy 对外暴露端口
    mediamtx_internal_port: int = 18004  # MediaMTX 实际监听端口

    # Gateway / 安全
    gateway_enabled: bool = True
    gateway_allowed_ips: str = ""            # 逗号分隔白名单，空=不限制
    gateway_rate_limit: int = 60             # 普通路径每窗口最大请求数
    gateway_rate_window: int = 60            # 速率窗口大小（秒）
    gateway_rate_ban_threshold: int = 5      # 速率超限违规次数阈值（达到后封禁，0=不封禁）
    gateway_rate_ban_window: int = 60        # 速率超限违规计数窗口（秒）
    gateway_relaxed_prefixes: str = "/health,/task/message,/admin-f3m8,/metrics"  # 宽松路径前缀（逗号分隔）
    gateway_relaxed_rate_limit: int = 600    # 宽松路径每窗口最大请求数
    gateway_bypass_prefixes: str = "/media"  # 完全绕过速率限制与反扫描的前缀（仅靠路由层 token 鉴权），逗号分隔
    gateway_scan_threshold: int = 10         # 触发封禁的 404/405 次数（路径/方法枚举扫描）
    gateway_scan_window: int = 300           # 扫描计数窗口（秒）
    gateway_ban_duration: int = 3600         # 封禁时长（秒）

    # 媒体追溯（traceback）配置
    media_token_secret: str = ""             # 媒体 URL HMAC 签名密钥（空则启动时生成随机临时密钥）
    media_token_ttl: int = 300               # 媒体 token 有效期（秒）
    traceback_context_before: int = 1        # 告警证据：触发段之前的上下文段数
    traceback_context_after: int = 2         # 告警证据：触发段之后的上下文段数

    # Lab / Label Studio 视频段导出
    label_studio_url: str = ""                # LS 服务器 base URL，如 http://10.176.122.22:8080
    label_studio_token: str = ""              # LS Legacy Token（Authorization: Token <...>）
    label_studio_default_project_id: int = 0  # 默认 project_id；0 表示未配置（请求需显式传 project_id）
    lab_export_temp_dir: str = ""             # 临时输出目录；空则用 {storage_base_dir}/.lab_exports
    lab_export_ffmpeg_preset: str = "veryfast"
    lab_export_max_clip_ms: int = 300_000     # 单段时长上限（5 min）
    lab_export_max_total_ms: int = 1_800_000  # 一次提交总时长上限（30 min）
    lab_export_max_clips_per_submit: int = 20
    lab_export_gap_tolerance_ms: int = 2000   # 相邻段间隔相对 step 实测节奏的允许超出量；>此值判为真录制停顿（源断流/重连）

    @property
    def inference_fps(self) -> float:
        """检测抽帧后的有效帧率（派生：raw_fps / inference_decimation）。

        供需要绝对速率的消费者读（如 viz 轮询率）；抽帧器本身只用整数倍率
        inference_decimation「每 N 帧留 1」，不做此除法。派生化后无从被设成与
        raw_fps/N 不一致的值（消漂移）。N 非整除 raw_fps 时为小数（如 30/4=7.5）。
        """
        return self.raw_fps / self.inference_decimation

    @property
    def allowed_ips_set(self) -> frozenset:
        """解析 gateway_allowed_ips 为 frozenset，空字符串返回空集合（不限制）"""
        if not self.gateway_allowed_ips.strip():
            return frozenset()
        return frozenset(ip.strip() for ip in self.gateway_allowed_ips.split(",") if ip.strip())

    @property
    def env(self) -> str:
        """当前环境（dev/test/prod），由启动脚本通过 CLEANSIGHT_ENV 设定"""
        return os.environ.get("CLEANSIGHT_ENV", "dev").lower()

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def storage_base_dir(self) -> Path:
        """持久化存储根目录（绝对路径，单一真源）。

        相对路径以项目根为基，避免读写两侧因进程 cwd 不同而分叉到不同目录。
        persistence / inference / traceback 三方都读此值。
        """
        p = Path(self.storage_dir)
        if p.is_absolute():
            return p.resolve()
        return (Path(__file__).parent.parent / p).resolve()

    @model_validator(mode="after")
    def check_required_fields(self):
        """验证必需配置是否完整

        - 严格模式（CLEANSIGHT_STRICT=1）：缺失配置时抛出异常，阻止启动
        - 开发模式（CLEANSIGHT_STRICT=0）：只警告，允许继续运行
        """
        is_dev = globals().get("_LOADED_DEV", False)

        # 检查必需配置
        missing_fields = []

        # 数据库配置
        if not self.db_host:
            missing_fields.append("CLEANSIGHT_DB_HOST")
        if not self.db_port or self.db_port == 0:
            missing_fields.append("CLEANSIGHT_DB_PORT")
        if not self.db_name:
            missing_fields.append("CLEANSIGHT_DB_NAME")
        if not self.db_user:
            missing_fields.append("CLEANSIGHT_DB_USER")
        if not self.db_password:
            missing_fields.append("CLEANSIGHT_DB_PASSWORD")

        # 外部接口URL配置
        if not self.alarm_report_url:
            missing_fields.append("CLEANSIGHT_ALARM_REPORT_URL")

        if missing_fields:
            msg = f"缺少必需配置: {', '.join(missing_fields)}"

            if self.strict and not is_dev:
                # 生产模式且开启严格检查：抛出异常
                raise ValueError(
                    f"[配置错误] {msg}\n"
                    f"请检查环境变量或 .env 文件\n"
                    f"参考 .env.example 文件查看所需配置"
                )
            else:
                # 开发模式或未开启严格检查：只警告
                print(f"\n{'='*60}")
                print(f"[Settings] ⚠️  警告: {msg}")
                print(f"[Settings] 当前为开发模式，允许继续运行")
                print(f"[Settings] 部分功能（如数据库操作）可能不可用")
                print(f"{'='*60}\n")

        return self

    @model_validator(mode="after")
    def _resolve_ffmpeg_path(self):
        """ffmpeg_path 为空时指向项目自包含的钉版静态包，消除「install 装到固定位置却要人手抄进 .env」的负担：

        - 项目自包含第三方服务：ffmpeg 与 mediamtx 同为项目内 vendored 二进制，唯一来源是
          项目根下的 .ffmpeg/bin/（install.sh/install.ps1 部署；Linux 名 ffmpeg，Windows 名 ffmpeg.exe）。
          钉版保证 HLS fmp4 行为正确，见 docs/HLS_TIMELINE_PITFALL.md。
        - 不回退 PATH：一处明确来源、失败即报（缺料时由 FFmpegDecoder 抛 FFmpegError，
          报出确切路径，提示先跑 install 脚本）。
        - 显式设了 CLEANSIGHT_FFMPEG_PATH 则尊重之（逃生口，如 Mac 开发机指向 homebrew ffmpeg）。

        路径与 install 脚本各自从「项目根」推导 .ffmpeg/bin/，无共享绝对路径耦合。
        """
        if not self.ffmpeg_path:
            bin_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            self.ffmpeg_path = str(
                Path(__file__).parent.parent / ".ffmpeg" / "bin" / bin_name
            )
        return self

    model_config = SettingsConfigDict(
        env_prefix="CLEANSIGHT_",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
    )


# 在实例化 Settings 前，先把 .env/.env.dev 加载到环境中
_load_env_files()
settings = Settings()

# YOLO_CONFIG_DIR 是 ultralytics 的全局变量；系统级设置会劫持同机其他模型任务。
# 故仅在本进程内锁死为项目内 .ultralytics（由安装位置自动得出、必然可写），不外溢、不可配。
_yolo_cfg_dir = str(Path(__file__).parent.parent / ".ultralytics")
os.makedirs(_yolo_cfg_dir, exist_ok=True)
os.environ["YOLO_CONFIG_DIR"] = _yolo_cfg_dir

# ultralytics 的 predictor.__init__ 无条件 mkdir(save_dir)（即便 save=False），
# 默认落在仓库根 runs/detect/，每次 warmup/推理都残留空目录。把 predict 的 project
# 钉进上面已 gitignore 的 .ultralytics，令这些空目录不再污染仓库根。detector 引用此常量。
YOLO_RUNS_PROJECT = str(Path(_yolo_cfg_dir) / "runs" / "detect")
