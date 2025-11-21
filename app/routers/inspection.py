from fastapi import APIRouter, WebSocket
from app.services import ai

router = APIRouter(prefix="/inspection", tags=["inspection"])


@router.websocket("/upload_stream")
async def websocket_upload_stream(websocket: WebSocket):
    """
    WebSocket端点：/inspection/upload_stream?client_id=xxx
    实时接收摄像头上传的视频帧（Base64编码）并加入处理队列。
    - 要求在连接时通过查询参数提供 `client_id`
    - 客户端持续发送帧，服务器确认接收。
    """
    client_id = websocket.query_params.get("client_id")
    if not client_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    print(f"WebSocket 上传连接已建立 (client_id={client_id}): {websocket.client}")

    try:
        while True:
            # 接收客户端发送的帧（Base64字符串）
            frame_data = await websocket.receive_text()

            # 直接使用 ai 提供的提交接口
            result = ai.submit_base64_frame(client_id, frame_data)

            if result == "success":
                await websocket.send_text("success")
            else:
                await websocket.send_text(f"error: {result}")

    except Exception as e:
        print(f"WebSocket 上传错误 (client_id={client_id}): {e}")
    finally:
        ai.remove_client(client_id)
        print(f"WebSocket 上传连接已关闭 (client_id={client_id}): {websocket.client}")