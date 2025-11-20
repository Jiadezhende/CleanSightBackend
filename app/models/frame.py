"""
Data models used to collect AI inference results and store task state.
"""
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime

class LabeledFrame(BaseModel):
    client_id: str
    timestamp: float
    processed_frame_b64: str  # Base64 encoded JPEG image
    metadata: Optional[Dict[str, Any]] = None  # Additional metadata if needed

# 用于存储处理后帧和推理结果
class ProcessedFrame(BaseModel):
    id: Optional[int] = None  # 数据库主键
    task_id: str
    frame_number: int
    processed_frame_b64: str  # 标注好的视频帧 (Base64)
    inference_result: Dict[str, Any]  # 推理结果 JSON
    created_at: datetime = datetime.now()

class RawFrame(BaseModel):
    id: Optional[int] = None  # 数据库主键
    task_id: str
    frame_number: int
    raw_frame_b64: str  # 原始视频帧 (Base64)
    created_at: datetime = datetime.now()