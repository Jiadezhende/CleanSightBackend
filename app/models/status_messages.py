"""
任务状态和消息字典表

提供状态码、状态描述和前端显示消息的映射
"""

from typing import Dict, List, Optional
from pydantic import BaseModel


class StatusMessage(BaseModel):
    """状态消息模型"""
    status_code: str
    status_text: str
    message: str
    severity: str  # info, warning, error, success


# 任务状态字典
TASK_STATUS_DICT: Dict[str, StatusMessage] = {
    "idle": StatusMessage(
        status_code="idle",
        status_text="空闲",
        message="当前无任务运行",
        severity="info"
    ),
    "running": StatusMessage(
        status_code="running",
        status_text="运行中",
        message="清洗任务正在执行",
        severity="success"
    ),
    "paused": StatusMessage(
        status_code="paused",
        status_text="已暂停",
        message="任务已暂停",
        severity="warning"
    ),
    "completed": StatusMessage(
        status_code="completed",
        status_text="已完成",
        message="清洗任务已完成",
        severity="success"
    ),
    "error": StatusMessage(
        status_code="error",
        status_text="错误",
        message="任务执行出现错误",
        severity="error"
    ),
    "terminated": StatusMessage(
        status_code="terminated",
        status_text="已终止",
        message="任务已被终止",
        severity="info"
    )
}


# 清洗步骤字典
CLEANING_STEP_DICT: Dict[str, str] = {
    "0": "准备阶段",
    "1": "预冲洗",
    "2": "酶洗",
    "3": "主清洗",
    "4": "漂洗",
    "5": "终末漂洗",
    "6": "干燥",
    "7": "完成"
}


# 异常检测消息生成器
def generate_alert_messages(
    bending: bool = False,
    bubble_detected: bool = False,
    fully_submerged: bool = True,
    bending_count: int = 0
) -> List[str]:
    """
    根据检测结果生成告警消息列表
    
    Args:
        bending: 是否检测到弯折
        bubble_detected: 是否检测到气泡（漏气）
        fully_submerged: 是否完全浸没
        bending_count: 弯折计数
        
    Returns:
        告警消息列表
    """
    messages = []
    
    # 漏气检测
    if bubble_detected:
        messages.append("检测到气泡，可能存在漏气！请检查管路密封性")

    # 弯折检测（只提示是否需要弯曲，不包含次数）
    if bending:
        messages.append("需要弯曲软管")

    # 浸没检测
    if not fully_submerged:
        messages.append("内镜未完全浸没，请调整位置")
    
    # 如果没有异常
    if not messages:
        messages.append("设备运行正常")
    
    return messages


def get_task_status_response(
    task_id: int,
    status: str,
    current_step: str,
    bending: bool = False,
    bubble_detected: bool = False,
    fully_submerged: bool = True,
    bending_count: int = 0,
    updated_at: str = None # type: ignore
) -> Dict:
    """
    生成统一的任务状态响应
    
    Args:
        task_id: 任务ID
        status: 任务状态
        current_step: 当前清洗步骤
        bending: 是否弯折
        bubble_detected: 是否检测到气泡
        fully_submerged: 是否完全浸没
        bending_count: 弯折计数
        updated_at: 更新时间
        
    Returns:
        格式化的状态响应字典
    """
    # 获取状态信息
    status_info = TASK_STATUS_DICT.get(
        status,
        StatusMessage(
            status_code=status,
            status_text="未知状态",
            message=f"状态: {status}",
            severity="info"
        )
    )
    
    # 获取清洗步骤名称
    step_name = CLEANING_STEP_DICT.get(current_step, f"步骤 {current_step}")
    
    # 生成告警消息
    alert_messages = generate_alert_messages(
        bending=bending,
        bubble_detected=bubble_detected,
        fully_submerged=fully_submerged,
        bending_count=bending_count
    )
    
    return {
        "task_id": task_id,
        "status": {
            "code": status_info.status_code,
            "text": status_info.status_text,
            "message": status_info.message,
            "severity": status_info.severity
        },
        "cleaning_step": {
            "code": current_step,
            "name": step_name
        },
        "detection": {
            "bending": bending,
            "bubble_detected": bubble_detected,
            "fully_submerged": fully_submerged
        },
        "messages": alert_messages,
        "updated_at": updated_at
    }


def get_no_task_response() -> Dict:
    """返回无任务的响应"""
    return {
        "task_id": None,
        "status": {
            "code": "idle",
            "text": "空闲",
            "message": "当前无活跃任务",
            "severity": "info"
        },
        "cleaning_step": None,
        "detection": None,
        "messages": ["等待任务启动"],
        "updated_at": None
    }
