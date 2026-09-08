import os
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_env_files():
    """按 CLEANSIGHT_ENV 把对应 .env 文件的键值注入 `os.environ`，供 Settings 读取。

    dev→.env.dev（默认）/ test→.env.test / prod→.env。
    """
    base = Path(__file__).parent.parent
    env = os.environ.get("CLEANSIGHT_ENV", "dev").lower()

    env_files = {"dev": ".env.dev", "test": ".env.test", "prod": ".env"}
    env_file_name = env_files.get(env, ".env.dev")
    env_path = base / env_file_name

    global _LOADED_DEV  # 供 check_required_fields 判断是否放行缺配置
    _LOADED_DEV = env == "dev"

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

    # 持久化存储根（单一真源）。env: CLEANSIGHT_STORAGE_DIR
    storage_dir: str = "./database"

    # 帧率与队列（跨模块单一真源，inference / stream / client / persistence 共读）。
    raw_fps: int = 30              # 解码 CFR 帧率，下游一切帧率/帧数换算的基准
    inference_decimation: int = 2  # 系统唯一采样旋钮：抽帧器「每 N 帧留 1」。整数倍率，故只能
                                   # 命中 raw_fps 的整除率（30→15/10/7.5…，不支持 30→20）
    # 以秒声明（时间是跨子系统货币），帧数在各消费边界按 raw_fps 换算。
    ca_maxlen_seconds: int = 30    # CA 队列缓存时长
    ca_segment_seconds: int = 10   # HLS 段时长

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
    # 宽松路径前缀（逗号分隔）。大屏三条必须在列：/task/live 与 /task/history 是跨 origin
    # 轮询，CORS 预检与实际请求各计一次，普通配额撑不住；/traceback 的 404 是正常业务态
    # （只落 raw 的 step 按默认 track=processed 查即 404），不该被反扫描当特征累计。
    gateway_relaxed_prefixes: str = (
        "/health,/task/message,/task/live,/task/history,/traceback,/admin-f3m8,/metrics"
    )
    gateway_relaxed_rate_limit: int = 600    # 宽松路径每窗口最大请求数
    gateway_bypass_prefixes: str = "/media"  # 完全绕过速率限制与反扫描的前缀（仅靠路由层 token 鉴权），逗号分隔
    gateway_scan_threshold: int = 10         # 触发封禁的 404/405 次数（路径/方法枚举扫描）
    gateway_scan_window: int = 300           # 扫描计数窗口（秒）
    gateway_ban_duration: int = 3600         # 封禁时长（秒）

    # 媒体追溯（traceback）配置
    media_token_secret: str = ""             # 媒体 URL HMAC 签名密钥（空则启动时生成随机临时密钥）
    media_token_ttl: int = 300               # 媒体 token 有效期（秒）

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
        """检测抽帧后的有效帧率。派生而非配置项，故无从与 raw_fps/N 漂移。

        供需要绝对速率的消费者读（如 viz 轮询率）；抽帧器本身只用整数倍率，不做此除法。
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
        """持久化存储根（绝对路径，单一真源）。

        相对路径以项目根为基，避免读写两侧因进程 cwd 不同而分叉。
        **只对 step_store 可见**，由门禁 test_storage_root_is_private_to_step_store 锁死。
        """
        p = Path(self.storage_dir)
        if p.is_absolute():
            return p.resolve()
        return (Path(__file__).parent.parent / p).resolve()

    @property
    def lab_export_root(self) -> Path:
        """lab 导出产物的落地根（clip 的 job_dir 与整段导出的 mp4）。

        在存储根**旁边**、与任何 (task_id, step_id) 无关，故派生在此而非向 step_store
        借根。前导点让 `step_store.tasks()` 的数字目录判据自然跳过它。
        """
        if self.lab_export_temp_dir.strip():
            return Path(self.lab_export_temp_dir)
        return self.storage_base_dir / ".lab_exports"

    @property
    def lab_runtime_config_path(self) -> Path:
        """lab 运行时配置文件。与 `lab_export_root` 同款：在存储根旁边，不经 step_store。"""
        return self.storage_base_dir / "lab_runtime_config.json"

    @property
    def config_dir(self) -> Path:
        """服务配置 yaml 目录（项目根 `config/`，绝对路径，单一真源）。

        各服务 config.py 一律读此值，不再各自数 `__file__` 层级——那种写法在文件
        挪窝时会静默指错目录，且五处各数各的。
        """
        return (Path(__file__).parent.parent / "config").resolve()

    @model_validator(mode="after")
    def check_required_fields(self):
        """校验必需配置。strict 且非 dev 时缺配置即抛异常阻止启动，否则只告警放行。"""
        is_dev = globals().get("_LOADED_DEV", False)

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
                raise ValueError(
                    f"[配置错误] {msg}\n"
                    f"请检查环境变量或 .env 文件\n"
                    f"参考 .env.example 文件查看所需配置"
                )
            else:
                print(f"\n{'='*60}")
                print(f"[Settings] ⚠️  警告: {msg}")
                print(f"[Settings] 当前为开发模式，允许继续运行")
                print(f"[Settings] 部分功能（如数据库操作）可能不可用")
                print(f"{'='*60}\n")

        return self

    @model_validator(mode="after")
    def _resolve_ffmpeg_path(self):
        """留空则指向项目自包含的钉版 `.ffmpeg/bin/`（install 脚本部署），免去手抄进 .env。

        不回退 PATH：一处来源、失败即报（缺料时 FFmpegDecoder 抛 FFmpegError 并报出路径）。
        钉版是 HLS fmp4 行为正确的前提，见 docs/HLS_TIMELINE_PITFALL.md。
        显式设 CLEANSIGHT_FFMPEG_PATH 则尊重之（逃生口，如 Mac 开发机指 homebrew）。
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


# 必须先于 Settings()：pydantic 只读 os.environ，不认 .env 文件本身。
_load_env_files()
settings = Settings()

# YOLO_CONFIG_DIR 是 ultralytics 的全局变量；系统级设置会劫持同机其他模型任务。
# 故仅在本进程内锁死为项目内 .ultralytics（由安装位置自动得出、必然可写），不外溢、不可配。
_yolo_cfg_dir = str(Path(__file__).parent.parent / ".ultralytics")
os.makedirs(_yolo_cfg_dir, exist_ok=True)
os.environ["YOLO_CONFIG_DIR"] = _yolo_cfg_dir

# ultralytics 的 predictor.__init__ 无条件 mkdir(save_dir)（即便 save=False），默认落在
# 仓库根 runs/detect/。钉进已 gitignore 的 .ultralytics，免得每次推理都污染仓库根。
YOLO_RUNS_PROJECT = str(Path(_yolo_cfg_dir) / "runs" / "detect")
