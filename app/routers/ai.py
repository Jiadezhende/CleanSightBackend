import threading
import asyncio
from contextlib import asynccontextmanager
from fastapi import APIRouter, WebSocket
from app.services import ai
from typing import cast
from app.models.frame import ProcessedFrame

router = APIRouter(prefix="/ai", tags=["ai"])


@asynccontextmanager
async def lifespan():
    """AI服务生命周期管理：启动/停止推理管理器"""
    ai.start()
    try:
        yield
    finally:
        ai.stop()


@router.websocket("/video")
async def websocket_video_endpoint(websocket: WebSocket):
    """
    WebSocket端点：/ai/video?client_id=xxx
    - 要求客户端在连接时通过查询参数 `client_id` 指定自身 ID
    - 服务器持续向该 client_id 推送最新推理结果（Base64 JPEG）
    """
    # 获取 client_id
    client_id = websocket.query_params.get("client_id")
    if not client_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    print(f"WebSocket 连接已建立 (client_id={client_id}): {websocket.client}")

    try:
        while True:
            processed_frame :ProcessedFrame= ai.get_result(client_id, as_model=True) # type: ignore

            if processed_frame is None:
                await asyncio.sleep(0.03)
                continue

            # 使用模型中的 Base64 编码图像
            data_url = f"data:image/jpeg;base64,{processed_frame.processed_frame_b64}"
            await websocket.send_text(data_url)

    except Exception as e:
        print(f"WebSocket 错误 (client_id={client_id}): {e}")
    finally:
        # 客户端断开时清理缓存（可选）
        ai.remove_client(client_id)
        print(f"WebSocket 连接已关闭 (client_id={client_id}): {websocket.client}")

@router.get("/status")
async def get_ai_status():
    """获取AI服务状态，返回详细的队列信息"""
    return ai.status()


def start_background_threads():
    # 启动由 ai 管理器负责的推理线程
    ai.start()
    print("AI后台线程已启动（多客户端推理管理器）")