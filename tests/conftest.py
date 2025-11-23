# conftest.py - Pytest fixtures for testing
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # Add project root to path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, get_db
from app.config import settings
import tempfile
from pathlib import Path

# Import all models to ensure they are registered with Base
from app.models.task import DBTask
from app.models.frame import HLSSegment

# Use in-memory SQLite for tests to avoid affecting real DB
TEST_DATABASE_URL = "sqlite:///:memory:"

@pytest.fixture(scope="session")
def engine():
    """Create test engine."""
    test_engine = create_engine(TEST_DATABASE_URL, echo=False)
    # Force drop all tables first
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)  # Create tables
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)  # Clean up

@pytest.fixture(scope="function")
def db_session(engine):
    """Provide a test database session."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

@pytest.fixture(scope="function")
def temp_dir():
    """Provide a temporary directory for file operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)