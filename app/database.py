from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

# 创建数据库引擎
engine = create_engine(settings.database_url, echo=settings.debug)

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明式基类，用于定义模型
Base = declarative_base()

# 数据库依赖注入函数
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()