# tests/test_db_connection.py
"""Test database connection."""
import pytest
from sqlalchemy import text
from app.database import get_db

def test_database_connection(db_session):
    """Test if we can connect to the database and execute a simple query."""
    try:
        # Execute a simple query
        result = db_session.execute(text("SELECT 1 as test"))
        row = result.fetchone()
        assert row is not None
        assert row[0] == 1
    except Exception as e:
        pytest.fail(f"Database connection failed: {e}")