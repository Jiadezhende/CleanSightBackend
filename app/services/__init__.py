# services package
from . import ai
from .ai_models import motion, detection

# 导出 ClientManager 单例
try:
    from .client_manager import client_manager
except ImportError:
    client_manager = None
