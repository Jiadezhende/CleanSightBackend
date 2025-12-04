"""
用于清洗任务控制
包括初始化、终止任务等功能
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from app.services import ai
from app.models.frame import HLSSegment
from app.models.status_messages import get_task_status_response, get_no_task_response
from app.database import get_db
import json
from datetime import datetime
from pathlib import Path

router = APIRouter(prefix="/task", tags=["task"])

@router.websocket("/status/{client_id}")
async def websocket_task_status(websocket: WebSocket, client_id: str):
    """
    实时更新任务状态的WebSocket接口
    
    返回格式化的状态信息，包含：
    - 任务状态（运行中/暂停/完成等）和对应的显示文本
    - 当前清洗步骤和步骤名称
    - 异常检测结果（弯折、漏气、浸没）
    - 前端可直接显示的消息列表
    
    Args:
        client_id: 客户端ID（也可以理解为摄像机ip/source_id）
    """
    await websocket.accept()
    try:
        while True:
            # 获取当前任务状态
            current_task = ai.get_task(client_id)

            if current_task:
                # 使用字典表生成格式化的状态响应
                status_data = get_task_status_response(
                    task_id=current_task.task_id,
                    status=current_task.status,
                    current_step=current_task.current_step,
                    bending=current_task.bending,
                    bubble_detected=current_task.bubble_detected,
                    fully_submerged=current_task.fully_submerged,
                    bending_count=getattr(current_task, 'bending_count', 0),
                    updated_at=datetime.fromtimestamp(current_task.updated_at).isoformat()
                )
                await websocket.send_text(json.dumps(status_data, ensure_ascii=False))
            else:
                # 没有活跃任务
                no_task_data = get_no_task_response()
                await websocket.send_text(json.dumps(no_task_data, ensure_ascii=False))

            # 每秒更新一次状态
            import asyncio
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        print(f"WebSocket 任务状态连接已关闭: {client_id}")
    except Exception as e:
        print(f"WebSocket 任务状态错误 for {client_id}: {e}")
        try:
            await websocket.close()
        except:
            pass


@router.get("/traceback/{task_id}/segments")
async def get_task_segments(task_id: int, video_type: str = "processed"):
    """
    获取任务的所有 HLS 视频段路径和关键点 JSON 路径
    
    Args:
        task_id: 任务 ID
        video_type: 视频类型 ("raw" 原始视频 或 "processed" 处理后视频)
    
    Returns:
        包含所有段信息的 JSON，包括视频路径、关键点路径、时间戳等
    """
    db = next(get_db())
    try:
        # 查询该任务的所有 HLS 段，按时间排序
        segments = db.query(HLSSegment).filter(
            HLSSegment.task_id == task_id
        ).order_by(HLSSegment.start_ts).all()
        
        if not segments:
            raise HTTPException(status_code=404, detail=f"未找到任务 {task_id} 的视频段")
        
        # 根据视频类型过滤
        filtered_segments = []
        for seg in segments:
            segment_path = str(seg.segment_path)  # type: ignore
            
            # 判断是否为目标类型的视频
            if video_type == "raw" and "raw_segment" in segment_path:
                filtered_segments.append(seg)
            elif video_type == "processed" and "processed_segment" in segment_path:
                filtered_segments.append(seg)
        
        if not filtered_segments:
            raise HTTPException(
                status_code=404, 
                detail=f"未找到任务 {task_id} 的 {video_type} 类型视频段"
            )
        
        # 构建返回数据
        result = {
            "task_id": task_id,
            "video_type": video_type,
            "total_segments": len(filtered_segments),
            "playlist_path": str(filtered_segments[0].playlist_path),  # type: ignore
            "segments": []
        }
        
        for seg in filtered_segments:
            segment_info = {
                "segment_id": seg.id,  # type: ignore
                "segment_path": str(seg.segment_path),  # type: ignore
                "start_time": seg.start_ts.isoformat() if seg.start_ts else None,  # type: ignore
                "end_time": seg.end_ts.isoformat() if seg.end_ts else None,  # type: ignore
                "client_id": str(seg.client_id)  # type: ignore
            }
            
            # 添加对应的关键点 JSON 路径（如果是处理后的视频）
            if video_type == "processed":
                segment_path = Path(seg.segment_path)  # type: ignore
                # 推断关键点文件路径
                timestamp = segment_path.stem.replace("processed_segment_", "")
                keypoints_path = segment_path.parent / f"keypoints_{timestamp}.json"
                
                if keypoints_path.exists():
                    segment_info["keypoints_path"] = str(keypoints_path)
            
            result["segments"].append(segment_info)
        
        return result
        
    finally:
        db.close()


@router.get("/traceback/{task_id}/playlist")
async def get_task_playlist(task_id: int, video_type: str = "processed"):
    """
    获取任务的 HLS 播放列表文件 (.m3u8)
    
    Args:
        task_id: 任务 ID
        video_type: 视频类型 ("raw" 或 "processed")
    
    Returns:
        M3U8 播放列表文件
    """
    db = next(get_db())
    try:
        # 查询第一个段以获取播放列表路径
        segment = db.query(HLSSegment).filter(
            HLSSegment.task_id == task_id
        ).order_by(HLSSegment.start_ts).first()
        
        if not segment:
            raise HTTPException(status_code=404, detail=f"未找到任务 {task_id} 的视频段")
        
        playlist_path = Path(segment.playlist_path)  # type: ignore
        
        # 根据视频类型选择正确的播放列表
        if video_type == "raw":
            playlist_path = playlist_path.parent / "raw_playlist.m3u8"
        else:
            playlist_path = playlist_path.parent / "processed_playlist.m3u8"
        
        if not playlist_path.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"播放列表文件不存在: {playlist_path}"
            )
        
        return FileResponse(
            path=str(playlist_path),
            media_type="application/vnd.apple.mpegurl",
            filename=f"task_{task_id}_{video_type}.m3u8"
        )
        
    finally:
        db.close()


@router.get("/traceback/{task_id}/video/{segment_id}")
async def stream_video_segment(task_id: int, segment_id: int):
    """
    流式传输指定的视频段
    
    Args:
        task_id: 任务 ID
        segment_id: 段 ID
    
    Returns:
        视频文件流
    """
    db = next(get_db())
    try:
        # 查询段记录
        segment = db.query(HLSSegment).filter(
            HLSSegment.id == segment_id,
            HLSSegment.task_id == task_id
        ).first()
        
        if not segment:
            raise HTTPException(
                status_code=404, 
                detail=f"未找到段 {segment_id} (任务 {task_id})"
            )
        
        segment_path = Path(segment.segment_path)  # type: ignore
        
        if not segment_path.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"视频文件不存在: {segment_path}"
            )
        
        return FileResponse(
            path=str(segment_path),
            media_type="video/mp4",
            filename=segment_path.name
        )
        
    finally:
        db.close()


@router.get("/traceback/{task_id}/keypoints/{segment_id}")
async def get_keypoints_data(task_id: int, segment_id: int):
    """
    获取指定视频段的关键点 JSON 数据
    
    Args:
        task_id: 任务 ID
        segment_id: 段 ID
    
    Returns:
        关键点 JSON 数据
    """
    db = next(get_db())
    try:
        # 查询段记录
        segment = db.query(HLSSegment).filter(
            HLSSegment.id == segment_id,
            HLSSegment.task_id == task_id
        ).first()
        
        if not segment:
            raise HTTPException(
                status_code=404, 
                detail=f"未找到段 {segment_id} (任务 {task_id})"
            )
        
        segment_path = Path(segment.segment_path)  # type: ignore
        
        # 推断关键点文件路径
        if "processed_segment" in segment_path.name:
            timestamp = segment_path.stem.replace("processed_segment_", "")
            keypoints_path = segment_path.parent / f"keypoints_{timestamp}.json"
            
            if not keypoints_path.exists():
                raise HTTPException(
                    status_code=404, 
                    detail=f"关键点文件不存在: {keypoints_path}"
                )
            
            # 读取并返回 JSON 数据
            with keypoints_path.open('r', encoding='utf-8') as f:
                keypoints_data = json.load(f)
            
            return JSONResponse(content=keypoints_data)
        else:
            raise HTTPException(
                status_code=400, 
                detail="原始视频段没有关键点数据，请使用处理后的视频段"
            )
        
    finally:
        db.close()


@router.get("/traceback/{task_id}/all_keypoints")
async def get_all_keypoints(task_id: int):
    """
    获取任务的所有关键点数据（合并所有段的关键点）
    
    Args:
        task_id: 任务 ID
    
    Returns:
        所有关键点数据的列表
    """
    db = next(get_db())
    try:
        # 查询该任务的所有处理后视频段
        segments = db.query(HLSSegment).filter(
            HLSSegment.task_id == task_id
        ).order_by(HLSSegment.start_ts).all()
        
        if not segments:
            raise HTTPException(status_code=404, detail=f"未找到任务 {task_id} 的视频段")
        
        all_keypoints = []
        
        for seg in segments:
            segment_path = Path(seg.segment_path)  # type: ignore
            
            # 只处理处理后的视频段
            if "processed_segment" not in segment_path.name:
                continue
            
            # 读取关键点文件
            timestamp = segment_path.stem.replace("processed_segment_", "")
            keypoints_path = segment_path.parent / f"keypoints_{timestamp}.json"
            
            if keypoints_path.exists():
                with keypoints_path.open('r', encoding='utf-8') as f:
                    segment_keypoints = json.load(f)
                    all_keypoints.extend(segment_keypoints)
        
        if not all_keypoints:
            raise HTTPException(
                status_code=404, 
                detail=f"未找到任务 {task_id} 的关键点数据"
            )
        
        return {
            "task_id": task_id,
            "total_frames": len(all_keypoints),
            "keypoints": all_keypoints
        }
        
    finally:
        db.close()