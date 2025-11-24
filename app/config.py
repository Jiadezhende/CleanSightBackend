from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator

class Settings(BaseSettings):
    # 数据库配置 - 使用环境变量以确保安全
    db_host: str = ""
    db_port: int = 0
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    # AI 模型配置
    yolo_model_path: str = "/opt/homebrew/runs/detect/train5/weights/best.pt"
    yolo_conf_threshold: float = 0.25
    yolo_iou_threshold: float = 0.45

    # .env 文件配置样例
    # CLEANSIGHT_DB_HOST=
    # CLEANSIGHT_DB_PORT=
    # CLEANSIGHT_DB_NAME=
    # CLEANSIGHT_DB_USER=
    # CLEANSIGHT_DB_PASSWORD=
    # CLEANSIGHT_YOLO_MODEL_PATH=/path/to/your/model.pt
    # CLEANSIGHT_YOLO_CONF_THRESHOLD=0.25
    # CLEANSIGHT_YOLO_IOU_THRESHOLD=0.45
        
    # 构造完整的数据库URL
    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
    
    # 其他配置
    debug: bool = False
    
    @model_validator(mode='after')
    def check_required_fields(self):
        if not self.db_host or self.db_port == 0 or not self.db_name or not self.db_user or not self.db_password:
            raise ValueError("数据库配置字段未设置或无效，请检查环境变量或 .env 文件")
        return self
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CLEANSIGHT_",
        env_nested_delimiter="__",
        env_ignore_empty=True
    )

settings = Settings()