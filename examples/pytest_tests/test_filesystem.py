"""Filesystem-related example pytest tests."""

import os
import tempfile


def test_tempfile_creation():
    with tempfile.NamedTemporaryFile() as f:
        assert os.path.exists(f.name)


def test_directory_listing():
    entries = os.listdir(".")
    assert isinstance(entries, list)


def test_path_join():
    result = os.path.join("/usr", "local", "bin")
    assert result == "/usr/local/bin"
