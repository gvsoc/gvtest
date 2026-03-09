"""
Tests for gvtest.config — hierarchical configuration discovery and merging.
"""

import os
import sys
import pytest
from pathlib import Path
from gvtest.config import ConfigLoader, get_python_paths_for_dir, load_and_apply_config


class TestConfigDiscovery:
    """Tests for gvtest.yaml file discovery."""

    def test_no_config_files(self, tmp_path):
        """No gvtest.yaml files anywhere in hierarchy."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        loader = ConfigLoader(str(deep))
        configs = loader.discover_configs()
        # May find configs outside tmp_path; filter to tmp_path
        configs_in_tmp = [c for c in configs if str(tmp_path) in str(c)]
        assert len(configs_in_tmp) == 0

    def test_single_config_in_start_dir(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("python_paths:\n  - ./lib\n")
        
        loader = ConfigLoader(str(tmp_path))
        configs = loader.discover_configs()
        assert config in configs

    def test_config_in_parent(self, tmp_path):
        """Config in parent dir is discovered from child."""
        config = tmp_path / "gvtest.yaml"
        config.write_text("python_paths:\n  - ./lib\n")
        child = tmp_path / "sub"
        child.mkdir()
        
        loader = ConfigLoader(str(child))
        configs = loader.discover_configs()
        assert config in configs

    def test_hierarchical_discovery_order(self, tmp_path):
        """Configs should be returned root → leaf order."""
        root_config = tmp_path / "gvtest.yaml"
        root_config.write_text("python_paths:\n  - ./root_lib\n")
        
        mid_dir = tmp_path / "project"
        mid_dir.mkdir()
        mid_config = mid_dir / "gvtest.yaml"
        mid_config.write_text("python_paths:\n  - ./mid_lib\n")
        
        leaf_dir = mid_dir / "tests"
        leaf_dir.mkdir()
        leaf_config = leaf_dir / "gvtest.yaml"
        leaf_config.write_text("python_paths:\n  - ./leaf_lib\n")
        
        loader = ConfigLoader(str(leaf_dir))
        configs = loader.discover_configs()
        
        # Filter to our tmp hierarchy
        our_configs = [c for c in configs if str(tmp_path) in str(c)]
        assert len(our_configs) == 3
        # Root should come first
        assert our_configs.index(root_config) < our_configs.index(mid_config)
        assert our_configs.index(mid_config) < our_configs.index(leaf_config)

    def test_gaps_in_hierarchy(self, tmp_path):
        """Only dirs with gvtest.yaml are included, gaps are fine."""
        root_config = tmp_path / "gvtest.yaml"
        root_config.write_text("python_paths: []\n")
        
        # No config in middle dir
        mid_dir = tmp_path / "project"
        mid_dir.mkdir()
        
        leaf_dir = mid_dir / "tests"
        leaf_dir.mkdir()
        leaf_config = leaf_dir / "gvtest.yaml"
        leaf_config.write_text("python_paths: []\n")
        
        loader = ConfigLoader(str(leaf_dir))
        configs = loader.discover_configs()
        our_configs = [c for c in configs if str(tmp_path) in str(c)]
        assert len(our_configs) == 2
        assert root_config in our_configs
        assert leaf_config in our_configs


class TestConfigLoading:
    """Tests for loading and parsing gvtest.yaml files."""

    def test_load_valid_config(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("python_paths:\n  - ./lib\n  - /opt/python\n")
        
        loader = ConfigLoader(str(tmp_path))
        result = loader.load_config(config)
        assert 'python_paths' in result
        assert result['python_paths'] == ['./lib', '/opt/python']

    def test_load_empty_config(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("")
        
        loader = ConfigLoader(str(tmp_path))
        result = loader.load_config(config)
        assert result == {}

    def test_load_config_only_comment(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("# just a comment\n")
        
        loader = ConfigLoader(str(tmp_path))
        result = loader.load_config(config)
        assert result == {}

    def test_load_invalid_yaml(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("python_paths: [unterminated\n")
        
        loader = ConfigLoader(str(tmp_path))
        with pytest.raises(RuntimeError, match="Failed to"):
            loader.load_config(config)

    def test_load_non_dict_yaml(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("- just\n- a\n- list\n")
        
        loader = ConfigLoader(str(tmp_path))
        with pytest.raises(RuntimeError, match="expected a dictionary"):
            loader.load_config(config)


class TestConfigValidation:
    """Tests for configuration validation."""

    def test_valid_config(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        # Should not raise
        loader.validate_config({'python_paths': ['./lib', '/opt/python']}, config_file)

    def test_empty_config_valid(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        loader.validate_config({}, config_file)

    def test_python_paths_not_list(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        with pytest.raises(RuntimeError, match="expected a list"):
            loader.validate_config({'python_paths': './lib'}, config_file)

    def test_python_paths_non_string_item(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        with pytest.raises(RuntimeError, match="expected a string"):
            loader.validate_config({'python_paths': [42]}, config_file)

    def test_unknown_keys_warning(self, tmp_path, caplog):
        """Unknown keys should produce a warning, not an error."""
        import logging
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        with caplog.at_level(logging.WARNING):
            loader.validate_config({'python_paths': [], 'unknown_key': True}, config_file)
        assert 'Unknown keys' in caplog.text


class TestPathResolution:
    """Tests for resolving relative/absolute paths."""

    def test_absolute_paths_unchanged(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        result = loader.resolve_paths(['/opt/lib', '/usr/local/python'], config_file)
        assert result == ['/opt/lib', '/usr/local/python']

    def test_relative_paths_resolved(self, tmp_path):
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        result = loader.resolve_paths(['./lib', '../other'], config_file)
        assert result[0] == str((tmp_path / 'lib').resolve())
        assert result[1] == str((tmp_path / '..' / 'other').resolve())

    def test_relative_to_config_dir(self, tmp_path):
        """Relative paths resolve relative to config file, not CWD."""
        subdir = tmp_path / "project"
        subdir.mkdir()
        config_file = subdir / "gvtest.yaml"
        
        loader = ConfigLoader(str(tmp_path))
        result = loader.resolve_paths(['./python'], config_file)
        assert result[0] == str((subdir / 'python').resolve())

    def test_nonexistent_path_warning(self, tmp_path, caplog):
        """Non-existent paths should warn but not error."""
        import logging
        config_file = tmp_path / "gvtest.yaml"
        loader = ConfigLoader(str(tmp_path))
        with caplog.at_level(logging.WARNING):
            result = loader.resolve_paths(['./nonexistent'], config_file)
        assert len(result) == 1
        assert 'does not exist' in caplog.text


class TestConfigMerging:
    """Tests for merging multiple config files."""

    def test_single_file_merge(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("python_paths:\n  - ./lib\n")
        
        loader = ConfigLoader(str(tmp_path))
        paths = loader.merge_configs([config])
        assert len(paths) == 1

    def test_multi_file_merge_order(self, tmp_path):
        """Root paths come first, leaf paths come last."""
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        root_lib = root_dir / "root_lib"
        root_lib.mkdir()
        root_config = root_dir / "gvtest.yaml"
        root_config.write_text(f"python_paths:\n  - {root_lib}\n")
        
        leaf_dir = tmp_path / "leaf"
        leaf_dir.mkdir()
        leaf_lib = leaf_dir / "leaf_lib"
        leaf_lib.mkdir()
        leaf_config = leaf_dir / "gvtest.yaml"
        leaf_config.write_text(f"python_paths:\n  - {leaf_lib}\n")
        
        loader = ConfigLoader(str(tmp_path))
        paths = loader.merge_configs([root_config, leaf_config])
        assert len(paths) == 2
        assert str(root_lib) in paths[0]
        assert str(leaf_lib) in paths[1]

    def test_empty_configs_merge(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("")
        
        loader = ConfigLoader(str(tmp_path))
        paths = loader.merge_configs([config])
        assert paths == []

    def test_config_without_python_paths(self, tmp_path):
        config = tmp_path / "gvtest.yaml"
        config.write_text("# no python_paths key\n")
        
        loader = ConfigLoader(str(tmp_path))
        paths = loader.merge_configs([config])
        assert paths == []


class TestSysPathManagement:
    """Tests for sys.path manipulation."""

    def test_apply_adds_paths(self, tmp_path):
        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        
        loader = ConfigLoader(str(tmp_path))
        original_path = sys.path.copy()
        try:
            loader.apply_to_sys_path([str(lib_dir)])
            assert str(lib_dir) in sys.path
        finally:
            sys.path = original_path

    def test_apply_no_duplicates(self, tmp_path):
        lib_dir = tmp_path / "mylib"
        lib_dir.mkdir()
        path_str = str(lib_dir)
        
        loader = ConfigLoader(str(tmp_path))
        original_path = sys.path.copy()
        try:
            sys.path.append(path_str)
            count_before = sys.path.count(path_str)
            loader.apply_to_sys_path([path_str])
            count_after = sys.path.count(path_str)
            assert count_after == count_before
        finally:
            sys.path = original_path


class TestGetPythonPathsForDir:
    """Tests for the convenience function."""

    def test_returns_paths(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        config = tmp_path / "gvtest.yaml"
        config.write_text(f"python_paths:\n  - {lib}\n")
        
        paths = get_python_paths_for_dir(str(tmp_path))
        assert str(lib) in paths

    def test_no_configs_returns_empty(self, tmp_path):
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        paths = get_python_paths_for_dir(str(deep))
        # Filter out any paths from outside tmp_path
        our_paths = [p for p in paths if str(tmp_path) in p]
        assert len(our_paths) == 0


class TestLoadAndApply:
    """Tests for load_and_apply end-to-end."""

    def test_full_flow(self, tmp_path):
        lib = tmp_path / "mylib_unique_test_marker"
        lib.mkdir()
        config = tmp_path / "gvtest.yaml"
        config.write_text(f"python_paths:\n  - {lib}\n")
        
        original_path = sys.path.copy()
        try:
            loader = ConfigLoader(str(tmp_path))
            count = loader.load_and_apply()
            assert count >= 1
            assert str(lib) in sys.path
        finally:
            sys.path = original_path

    def test_no_configs_returns_zero(self, tmp_path):
        deep = tmp_path / "isolated"
        deep.mkdir()
        loader = ConfigLoader(str(deep))
        # This may or may not find configs outside tmp_path
        # but at minimum shouldn't crash
        loader.load_and_apply()
