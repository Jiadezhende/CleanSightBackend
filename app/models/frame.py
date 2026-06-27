"""Frame data models for AI inference pipeline.

These Pydantic models represent raw and processed frames as they will be
stored (e.g., later in a database). In in-memory realtime queues we keep
numpy arrays for performance, converting to Base64 only when persisting or
returning to clients.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np
from pydantic import BaseModel


@dataclass
class FrameData:
    """轻量级帧数据类，用于内存队列传递（避免 Base64 编码开销）"""

    timestamp: float  # Unix timestamp
    frame: np.ndarray  # 原始或处理后的帧（numpy 数组）
    inference_result: Optional[Dict[str, Any]] = None  # 完整推理结果


class BaseFrame(BaseModel):
    task_id: Optional[int] = None
    client_id: Optional[str] = None
    raw_timestamp: Optional[datetime] = None  # 原始视频帧写入时间戳
    width: Optional[int] = None
    height: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None  # Additional data (optional)


class ProcessedFrame(BaseFrame):
    """
    处理后帧数据，包含Base64编码的处理后图像及推理结果
    """

    processed_frame_b64: str  # Base64 encoded processed (annotated) frame
    inference_result: Optional[Dict[str, Any]] = None  # detection / analysis output


# SQLAlchemy models for database storage
from sqlalchemy import BigInteger, Column, Integer, String

from app.database import Base


class HLSSegment(Base):
    """file_path 表 ORM（无代码平台托管，_id 是平台主键 varchar）"""

    __tablename__ = "file_path"

    _id = Column(String, primary_key=True)  # 平台主键 (varchar)
    id = Column(BigInteger, nullable=False, index=True)  # 业务数字 ID
    client_id = Column(String, index=True)
    task_id = Column(BigInteger, index=True)
    segment_path = Column(String)
    playlist_path = Column(String)
    start_ts = Column(BigInteger)
    end_ts = Column(BigInteger)
    created_at = Column(BigInteger, default=lambda: int(time.time()))
