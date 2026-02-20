import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

from app.settings import settings

logger = logging.getLogger("app.database")

# 创建数据库引擎（带连接池配置）
engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    # 连接池配置
    poolclass=QueuePool,
    pool_size=5,  # 常驻连接数
    max_overflow=10,  # 临时连接数
    pool_timeout=30,  # 获取连接超时时间
    pool_recycle=3600,  # 连接回收时间（1小时）
    pool_pre_ping=True,  # 连接前测试（关键：自动重连）
)


# 添加连接监听器，记录连接状态
@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    logger.debug("Database connection established")


@event.listens_for(engine, "close")
def receive_close(dbapi_conn, connection_record):
    logger.debug("Database connection closed")


# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明式基类，用于定义模型
Base = declarative_base()


# 数据库依赖注入函数（带异常处理和重试）
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database session error: {e}")
        db.rollback()
        raise
    finally:
        db.close()
