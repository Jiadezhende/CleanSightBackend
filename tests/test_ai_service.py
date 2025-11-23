# tests/test_ai_service.py
"""Test AI service: multi-client frame upload, get results, update tasks, HLS persistence."""
import pytest
import numpy as np
import cv2
from unittest.mock import patch
from app.services.ai import InferenceManager, get_task_traceback
from app.models.task import Task as CleaningTask
from app.models.frame import HLSSegment

@pytest.fixture
def ai_manager(temp_dir):
    """Create AI manager with temp directory."""
    manager = InferenceManager(db_dir=str(temp_dir))
    yield manager
    manager.stop()

def test_multi_client_frame_upload(ai_manager):
    """Test uploading frames from 5 clients."""
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
        assert status["queues"][client]["rt_raw"] == 1  # One frame submitted

def test_get_inference_result(ai_manager):
    """Test getting inference results."""
    client_id = "test_client"
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    
    ai_manager.submit_frame(client_id, frame)
    
    # Start inference (mock detection to avoid actual model)
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, [])):
        ai_manager.start()
        import time
        time.sleep(0.1)  # Allow processing
        ai_manager.stop()
    
    # Get result
    result = ai_manager.get_result(client_id)
    assert result is not None
    assert isinstance(result, np.ndarray)

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
    """Test HLS segment generation and database recording."""
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
    for _ in range(ai_manager._ca_segment_len):
        ai_manager.submit_frame(client_id, frame)
    
    # Mock detection and trigger flush
    with patch('app.services.ai_models.detection.detect_keypoints', return_value=(frame, [])):
        ai_manager.start()
        import time
        time.sleep(0.2)  # Allow processing and flush
        ai_manager.stop()
    
    # Check if HLS segment was created in database
    hls_segments = db_session.query(HLSSegment).filter(HLSSegment.client_id == client_id).all()
    assert len(hls_segments) > 0
    segment = hls_segments[0]
    assert segment.task_id == 1
    assert segment.segment_path.endswith('.mp4')
    assert segment.playlist_path.endswith('.m3u8')

def test_traceback_interface(db_session):
    """Test traceback interface for task HLS."""
    # Insert test HLS segment
    segment = HLSSegment(
        client_id="test_client",
        task_id=1,
        segment_path="/path/to/segment.mp4",
        playlist_path="/path/to/playlist.m3u8",
        start_ts=None,
        end_ts=None
    )
    db_session.add(segment)
    db_session.commit()
    
    # Query traceback
    with patch('app.services.ai.get_db') as mock_get_db:
        mock_get_db.return_value = iter([db_session])
        playlist_path = get_task_traceback(1)
    assert playlist_path == "/path/to/playlist.m3u8"
    
    # Test non-existent task
    with patch('app.services.ai.get_db') as mock_get_db:
        mock_get_db.return_value = iter([db_session])
        assert get_task_traceback(999) is None

def test_real_frame_and_video_upload(ai_manager):
    """Test uploading real frames from test_frame.jpg and frames from test_video.mp4."""
    client_id = "real_client"
    
    # Load real frame from test_frame.jpg
    frame_path = "test/test_frame.jpg"
    frame = cv2.imread(frame_path)
    assert frame is not None, f"Failed to load frame from {frame_path}"
    
    # Submit the real frame
    ai_manager.submit_frame(client_id, frame)
    
    # Load frames from test_video.mp4
    video_path = "test/test_video.mp4"
    cap = cv2.VideoCapture(video_path)
    assert cap.isOpened(), f"Failed to open video {video_path}"
    
    frame_count = 0
    while frame_count < 5:  # Limit to 5 frames for test
        ret, video_frame = cap.read()
        if not ret:
            break
        ai_manager.submit_frame(client_id, video_frame)
        frame_count += 1
    
    cap.release()
    
    # Check status
    status = ai_manager.status()
    assert client_id in status["queues"]
    assert status["queues"][client_id]["rt_raw"] == 1 + frame_count  # 1 image + video frames