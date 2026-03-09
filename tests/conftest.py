"""
Shared fixtures for gvtest test suite.
"""

import os
import sys
import pytest
import tempfile
import shutil
from pathlib import Path

# Ensure gvtest package is importable
GVTEST_PYTHON = os.path.join(os.path.dirname(__file__), '..', 'python')
if GVTEST_PYTHON not in sys.path:
    sys.path.insert(0, os.path.abspath(GVTEST_PYTHON))


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace directory for test isolation."""
    return tmp_path


@pytest.fixture
def make_testset_file(tmp_workspace):
    """Factory fixture to create testset.cfg files with given content."""
    def _make(content, subdir=None):
        if subdir:
            path = tmp_workspace / subdir
            path.mkdir(parents=True, exist_ok=True)
        else:
            path = tmp_workspace
        
        testset_file = path / 'testset.cfg'
        testset_file.write_text(content)
        return str(testset_file)
    
    return _make


@pytest.fixture
def make_config_file(tmp_workspace):
    """Factory fixture to create gvtest.yaml config files."""
    def _make(content, subdir=None):
        if subdir:
            path = tmp_workspace / subdir
            path.mkdir(parents=True, exist_ok=True)
        else:
            path = tmp_workspace
        
        config_file = path / 'gvtest.yaml'
        config_file.write_text(content)
        return str(config_file)
    
    return _make


@pytest.fixture
def simple_runner():
    """Create a minimal Runner instance for unit testing."""
    from gvtest.runner import Runner
    runner = Runner(
        config='default',
        nb_threads=1,
        properties=[],
        flags=[],
    )
    return runner


@pytest.fixture
def runner_factory():
    """Factory to create Runner instances with custom params."""
    runners = []
    
    def _make(**kwargs):
        defaults = {
            'config': 'default',
            'nb_threads': 1,
            'properties': [],
            'flags': [],
        }
        defaults.update(kwargs)
        from gvtest.runner import Runner
        r = Runner(**defaults)
        runners.append(r)
        return r
    
    yield _make
    
    # Cleanup: stop any runners that were started
    for r in runners:
        try:
            r.stop()
        except:
            pass
