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
    file_path_insert_url: str
    alarm_report_url: str

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # 日志配置
    log_level: str = "INFO"
    log_config: str = "logging_config.json"

    # 外部工具
    ffmpeg_path: str = "ffmpeg"

    # 模型路径
    model_path: str = "./app/data"

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
    gateway_relaxed_prefixes: str = "/health,/task/message"  # 宽松路径前缀（逗号分隔）
    gateway_relaxed_rate_limit: int = 600    # 宽松路径每窗口最大请求数
    gateway_scan_threshold: int = 10         # 触发封禁的 404/405 次数（路径/方法枚举扫描）
    gateway_scan_window: int = 300           # 扫描计数窗口（秒）
    gateway_ban_duration: int = 3600         # 封禁时长（秒）

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
        if not self.file_path_insert_url:
            missing_fields.append("CLEANSIGHT_FILE_PATH_INSERT_URL")
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

    model_config = SettingsConfigDict(
        env_prefix="CLEANSIGHT_",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
    )


# 在实例化 Settings 前，先把 .env/.env.dev 加载到环境中
_load_env_files()
settings = Settings()
