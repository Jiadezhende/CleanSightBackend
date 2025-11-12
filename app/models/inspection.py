from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class InspectionResult(BaseModel):
    id: int
    image_path: str
    result: str  # e.g., "pass", "fail"
    confidence: float
    timestamp: datetime
    details: Optional[str] = None