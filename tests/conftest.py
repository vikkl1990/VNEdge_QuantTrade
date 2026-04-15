"""pytest configuration."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def empty_brain_memory(tmp_path):
    """Provide a fresh BrainMemory with isolated storage."""
    from bot.brain_memory import BrainMemory
    return BrainMemory(storage_dir=tmp_path)
