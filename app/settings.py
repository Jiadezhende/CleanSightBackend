import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator


def _load_env_files():
    """按优先级加载环境文件：
    - 若环境变量 `CLEANSIGHT_ENV_FILE` 指定路径，则加载该文件（优先）
    - 若环境变量 `CLEANSIGHT_PROD=1`，则加载 `.env` 作为生产环境配置
    - 否则默认加载 `.env.dev` 作为开发环境配置
    该函数会把键值对注入到 `os.environ`，以便 Pydantic 从环境读取。
    """
    base = Path(__file__).parent.parent
    env_file_override = os.environ.get('CLEANSIGHT_ENV_FILE')
    prod_mode = os.environ.get('CLEANSIGHT_PROD', '').lower() in ('1', 'true', 'yes')
    # 记录是否加载了 .env.dev（供后续校验逻辑判断是否为开发模式）
    global _LOADED_DEV
    _LOADED_DEV = False

    candidates = []
    if env_file_override:
        # 指定文件优先使用（单个文件）
        candidates.append(Path(env_file_override))
        _LOADED_DEV = False  # 自定义文件，不视为 dev 模式
    elif prod_mode:
        # 生产模式：只加载 .env
        prod_path = base / '.env'
        if prod_path.exists():
            candidates.append(prod_path)
        _LOADED_DEV = False
    else:
        # 开发模式（默认）：只加载 .env.dev
        dev_path = base / '.env.dev'
        if dev_path.exists():
            candidates.append(dev_path)
            _LOADED_DEV = True

    for p in candidates:
        try:
            p = Path(p)
            if not p.exists():
                continue
            with p.open('r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ[k] = v
        except Exception:
            # 不要在导入阶段让 .env 文件加载失败阻塞应用
            continue


class Settings(BaseSettings):
    # 数据库配置 - 使用环境变量以确保安全
    db_host: str = ""
    db_port: int = 0
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    # 其他配置
    debug: bool = False

    # file_path 插入接口（用于把 HLS 段信息发送到无代码平台）
    # 如果为空，则默认使用当前服务的 /api/file_path_insert
    file_path_insert_url: str = ""

    # 推理与视频配置
    inference_fps: int = 20  # 推理采样频率（同时控制可视化和视频输出帧率）
    ca_segment_seconds: int = 10  # 视频段时长（秒）

    # 调试配置
    show_debug_boxes: bool = False  # 是否在可视化中显示调试方框（用于确认模型加载）

    # 流管理配置
    stream_restart_interval: int = 30  # 推流断开后重启间隔（秒）
    stream_inactivity_timeout: int = 180  # 无活动自动清理超时（秒，默认3分钟）

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @model_validator(mode='after')
    def check_required_fields(self):
        """如果关键数据库字段缺失：
        - 当设置 `CLEANSIGHT_STRICT=1` 时，抛出异常（适用于生产环境）。
        - 否则仅打印警告以便本地开发继续运行（若加载了 `.env.dev` 则视为开发环境）。
        """
        strict = os.environ.get('CLEANSIGHT_STRICT', '') == '1'
        missing_db = (not self.db_host or self.db_port == 0 or not self.db_name or not self.db_user or not self.db_password)
        if missing_db:
            msg = "数据库配置字段未设置或无效，请检查环境变量或 .env 文件"
            # 如果开启严格模式并且不是加载的 dev 配置，则直接抛错
            if strict and not globals().get('_LOADED_DEV', False):
                raise ValueError(msg)
            else:
                print(f"[Settings] Warning: {msg}")
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