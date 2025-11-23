# tests/test_task_service.py
"""Test task service: creation and termination."""
import pytest
from unittest.mock import patch
from app.services.task import initialize_task, terminate_task
from app.models.task import DBTask

def test_initialize_task(db_session):
    """Test task initialization."""
    actor_id = 123
    
    # Mock get_db to return test session
    with patch('app.services.task.get_db') as mock_get_db:
        mock_get_db.return_value = iter([db_session])
        task = initialize_task(actor_id)
    
    assert task is not None
    assert task.initiator_operator_id == actor_id
    assert task.current_step == "0"
    assert task.status == "initialized"
    
    # Check database
    db_task = db_session.query(DBTask).filter(DBTask.task_id == task.task_id).first()
    assert db_task is not None
    assert db_task.initiator_operator_id == actor_id
    assert db_task.status == "initialized"

def test_terminate_task(db_session):
    """Test task termination."""
    # First create a task
    actor_id = 456
    with patch('app.services.task.get_db') as mock_get_db:
        mock_get_db.return_value = iter([db_session])
        task = initialize_task(actor_id)
    
    # Terminate it
    with patch('app.services.task.get_db') as mock_get_db:
        mock_get_db.return_value = iter([db_session])
        success = terminate_task(task)
    assert success is True
    
    # Check database
    db_task = db_session.query(DBTask).filter(DBTask.task_id == task.task_id).first()
    assert db_task.status == "terminated"