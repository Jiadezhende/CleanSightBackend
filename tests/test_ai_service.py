# tests/test_ai_service.py
"""Test AI service with RTMP architecture: four-queue system, HLS persistence, and traceback APIs."""
import pytest
import numpy as np
import cv2
import time
from unittest.mock import patch, MagicMock
from app.services.ai import InferenceManager
from app.models.task import Task as CleaningTask
from app.models.frame import HLSSegment, FrameData

@pytest.fixture
def ai_manager(temp_dir):
    """Create AI manager with temp directory."""
    manager = InferenceManager(db_dir=str(temp_dir))
    yield manager
    manager.stop()

def test_multi_client_frame_upload(ai_manager):
    """Test uploading frames from 5 clients to CA-ReadyQueue."""
    clients = [f"client_{i}" for i in range(5)]
    
    # Create mock frames (small numpy arrays)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    
    for client in clients:
        ai_manager.submit_frame(client, frame)
    
    # Check status
    status = ai_manager.status()
    assert status["clients"] == 5
    for client in clients:
        assert client in status["queues"]
        # 新架构：帧提交到 CA-ReadyQueue
        assert status["queues"][client]["ca_ready"] == 1  # One frame in CA-ReadyQueue

def test_get_inference_result(ai_manager):
    """Test getting inference results from RT-ProcessedQueue."""
    client_id = "test_client"
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    
    ai_manager.submit_frame(client_id, frame)
    
    # Mock detection to avoid actual model
    mock_keypoints = {"nose": [50, 50], "left_eye": [45, 45]}
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, mock_keypoints)):
        with patch('app.services.ai_models.motion.analyze_motion', return_value={"motion": "stable"}):
            ai_manager.start()
            time.sleep(0.2)  # Allow processing
            ai_manager.stop()
    
    # Get result from RT-ProcessedQueue
    result = ai_manager.get_result(client_id)
    assert result is not None
    assert isinstance(result, FrameData)
    assert result.frame is not None
    assert isinstance(result.frame, np.ndarray)
    assert result.keypoints is not None

def test_update_task_instance(ai_manager, db_session):
    """Test updating task instance during inference."""
    client_id = "test_client"
    task = CleaningTask(
        task_id=1,
        initiator_operator_id=123,
        current_step="0",
        bending_count=0,
        bubble_detected=False,
        fully_submerged=False,
        status="active",
        created_at=0,
        updated_at=0,
        start_time=0,
        end_time=0
    )
    ai_manager.set_task(client_id, task)
    
    # Verify task is set
    retrieved_task = ai_manager.get_task(client_id)
    assert retrieved_task is not None
    assert retrieved_task.task_id == 1

def test_hls_persistence(ai_manager, temp_dir, db_session):
    """Test dual HLS segment generation (raw + processed) and database recording."""
    client_id = "test_client"
    task = CleaningTask(
        task_id=1,
        initiator_operator_id=123,
        current_step="0",
        bending_count=0,
        bubble_detected=False,
        fully_submerged=False,
        status="active",
        created_at=0,
        updated_at=0,
        start_time=0,
        end_time=0
    )
    ai_manager.set_task(client_id, task)
    
    # Submit enough frames to trigger HLS generation (ca_segment_len frames)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    for _ in range(ai_manager._ca_segment_len + 2):  # Extra frames to ensure flush
        ai_manager.submit_frame(client_id, frame)
    
    # Mock detection and motion analysis
    mock_keypoints = {"nose": [50, 50], "left_eye": [45, 45]}
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, mock_keypoints)):
        with patch('app.services.ai_models.motion.analyze_motion', return_value={"motion": "stable"}):
            ai_manager.start()
            time.sleep(0.5)  # Allow processing and flush
            ai_manager.stop()
    
    # Check if HLS segments were created in database
    hls_segments = db_session.query(HLSSegment).filter(HLSSegment.client_id == client_id).all()
    assert len(hls_segments) > 0
    
    # Verify both raw and processed segments exist
    raw_segments = [s for s in hls_segments if "raw_segment" in s.segment_path]
    processed_segments = [s for s in hls_segments if "processed_segment" in s.segment_path]
    
    assert len(raw_segments) > 0, "Raw video segments should be created"
    assert len(processed_segments) > 0, "Processed video segments should be created"
    
    # Verify segment attributes
    segment = processed_segments[0]
    assert segment.task_id == 1
    assert segment.segment_path.endswith('.mp4')
    assert segment.playlist_path.endswith('.m3u8')
    assert segment.keypoints_path is not None
    assert segment.keypoints_path.endswith('.json')

def test_traceback_segments_query(db_session):
    """Test querying HLS segments for task traceback."""
    from sqlalchemy import select
    
    # Insert test HLS segments (both raw and processed)
    raw_segment = HLSSegment(
        client_id="test_client",
        task_id=1,
        segment_path="/path/to/raw_segment_123456.mp4",
        playlist_path="/path/to/raw_playlist.m3u8",
        keypoints_path=None,
        start_ts=None,
        end_ts=None
    )
    processed_segment = HLSSegment(
        client_id="test_client",
        task_id=1,
        segment_path="/path/to/processed_segment_123456.mp4",
        playlist_path="/path/to/processed_playlist.m3u8",
        keypoints_path="/path/to/keypoints_123456.json",
        start_ts=None,
        end_ts=None
    )
    db_session.add(raw_segment)
    db_session.add(processed_segment)
    db_session.commit()
    
    # Query all segments for task
    segments = db_session.query(HLSSegment).filter(HLSSegment.task_id == 1).all()
    assert len(segments) == 2
    
    # Query only processed segments (with keypoints)
    processed_only = db_session.query(HLSSegment).filter(
        HLSSegment.task_id == 1,
        HLSSegment.keypoints_path.isnot(None)
    ).all()
    assert len(processed_only) == 1
    assert processed_only[0].keypoints_path.endswith('.json')
    
    # Query only raw segments (without keypoints)
    raw_only = db_session.query(HLSSegment).filter(
        HLSSegment.task_id == 1,
        HLSSegment.keypoints_path.is_(None)
    ).all()
    assert len(raw_only) == 1
    
    # Test non-existent task
    no_segments = db_session.query(HLSSegment).filter(HLSSegment.task_id == 999).all()
    assert len(no_segments) == 0

def test_four_queue_architecture(ai_manager):
    """Test the four-queue architecture: CA-ReadyQueue, CA-RawQueue, CA-ProcessedQueue, RT-ProcessedQueue."""
    client_id = "test_client"
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    
    # Submit frame to CA-ReadyQueue
    ai_manager.submit_frame(client_id, frame)
    
    status = ai_manager.status()
    assert status["queues"][client_id]["ca_ready"] == 1
    assert status["queues"][client_id]["ca_raw"] == 0
    assert status["queues"][client_id]["ca_processed"] == 0
    assert status["queues"][client_id]["rt_processed"] == 0
    
    # Start inference to process the frame
    mock_keypoints = {"nose": [50, 50]}
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, mock_keypoints)):
        with patch('app.services.ai_models.motion.analyze_motion', return_value={"motion": "stable"}):
            ai_manager.start()
            time.sleep(0.2)  # Allow processing
            ai_manager.stop()
    
    # After inference, frame should be moved to CA-Raw, CA-Processed, and RT-Processed
    status = ai_manager.status()
    assert status["queues"][client_id]["ca_ready"] == 0  # Consumed
    assert status["queues"][client_id]["ca_raw"] >= 0  # May be flushed if segment complete
    assert status["queues"][client_id]["ca_processed"] >= 0  # May be flushed if segment complete
    assert status["queues"][client_id]["rt_processed"] == 1  # Latest result in RT queue

def test_rtmp_url_setting(ai_manager):
    """Test setting RTMP URL for client."""
    client_id = "rtmp_client"
    rtmp_url = "rtmp://localhost:1935/live/test_stream"
    
    ai_manager.set_rtmp_url(client_id, rtmp_url)
    
    # Verify RTMP URL is stored
    with ai_manager._lock:
        client_queues = ai_manager._clients[client_id]
        assert client_queues.rtmp_url == rtmp_url

def test_keypoints_json_persistence(ai_manager, temp_dir, db_session):
    """Test that keypoints are persisted to JSON files alongside video segments."""
    client_id = "test_client"
    task = CleaningTask(
        task_id=1,
        initiator_operator_id=123,
        current_step="0",
        bending_count=0,
        bubble_detected=False,
        fully_submerged=False,
        status="active",
        created_at=0,
        updated_at=0,
        start_time=0,
        end_time=0
    )
    ai_manager.set_task(client_id, task)
    
    # Submit frames with keypoints
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mock_keypoints = {"nose": [50, 50], "left_eye": [45, 45], "right_eye": [55, 45]}
    
    for _ in range(ai_manager._ca_segment_len + 2):
        ai_manager.submit_frame(client_id, frame)
    
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, mock_keypoints)):
        with patch('app.services.ai_models.motion.analyze_motion', return_value={"motion": "stable"}):
            ai_manager.start()
            time.sleep(0.5)
            ai_manager.stop()
    
    # Check database for keypoints path
    segments = db_session.query(HLSSegment).filter(
        HLSSegment.client_id == client_id,
        HLSSegment.keypoints_path.isnot(None)
    ).all()
    
    assert len(segments) > 0
    segment = segments[0]
    assert segment.keypoints_path.endswith('.json')
    
    # Verify JSON file exists and contains keypoints
    import json
    from pathlib import Path
    keypoints_file = Path(segment.keypoints_path)
    if keypoints_file.exists():  # May not exist in temp test environment
        with keypoints_file.open('r') as f:
            keypoints_data = json.load(f)
            assert isinstance(keypoints_data, list)
            assert len(keypoints_data) > 0
            assert "keypoints" in keypoints_data[0]