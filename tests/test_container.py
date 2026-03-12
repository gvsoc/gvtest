"""Tests for the container execution backend."""

import os
import pytest

from gvtest.container import ContainerConfig


class TestContainerConfig:
    """Tests for ContainerConfig construction and command building."""

    def test_basic_creation(self):
        c = ContainerConfig(image='ubuntu:22.04')
        assert c.image == 'ubuntu:22.04'
        assert c.runtime == 'docker'
        assert c.volumes == {}
        assert c.env == {}
        assert c.options == []
        assert c.setup is None
        assert c.workdir is None

    def test_full_creation(self):
        c = ContainerConfig(
            image='ghcr.io/org/image:tag',
            runtime='podman',
            volumes={'/data': '/data'},
            env={'FOO': 'bar'},
            options=['--gpus', 'all'],
            setup='pip install -e .',
            workdir='/app',
        )
        assert c.image == 'ghcr.io/org/image:tag'
        assert c.runtime == 'podman'
        assert c.volumes == {'/data': '/data'}
        assert c.env == {'FOO': 'bar'}
        assert c.options == ['--gpus', 'all']
        assert c.setup == 'pip install -e .'
        assert c.workdir == '/app'

    def test_from_dict_minimal(self):
        c = ContainerConfig.from_dict({'image': 'test:latest'})
        assert c.image == 'test:latest'
        assert c.runtime == 'docker'

    def test_from_dict_full(self):
        c = ContainerConfig.from_dict({
            'image': 'test:latest',
            'runtime': 'podman',
            'volumes': {'/src': '/src'},
            'env': {'KEY': 'val'},
            'options': ['--net=host'],
            'setup': 'make deps',
            'workdir': '/build',
        })
        assert c.image == 'test:latest'
        assert c.runtime == 'podman'
        assert c.volumes == {'/src': '/src'}
        assert c.env == {'KEY': 'val'}
        assert c.options == ['--net=host']
        assert c.setup == 'make deps'
        assert c.workdir == '/build'

    def test_from_dict_missing_image(self):
        with pytest.raises(ValueError, match="requires 'image'"):
            ContainerConfig.from_dict({'runtime': 'docker'})

    def test_from_dict_not_a_dict(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            ContainerConfig.from_dict("not a dict")

    def test_repr(self):
        c = ContainerConfig(image='test:latest')
        assert 'test:latest' in repr(c)
        assert 'docker' in repr(c)


class TestBuildRunCmd:
    """Tests for ContainerConfig.build_run_cmd()."""

    def test_basic_cmd(self):
        c = ContainerConfig(image='ubuntu:22.04')
        cmd = c.build_run_cmd('echo hello', cwd='/workspace')
        assert cmd[0] == 'docker'
        assert cmd[1] == 'run'
        assert cmd[2] == '--rm'
        assert 'ubuntu:22.04' in cmd
        assert cmd[-3:] == ['bash', '-c', 'echo hello']

    def test_transparent_mount(self):
        c = ContainerConfig(image='test:latest')
        cmd = c.build_run_cmd('ls', cwd='/workspace/project')
        # The cwd should be mounted at the same path
        real_cwd = os.path.realpath('/workspace/project')
        assert '-v' in cmd
        v_idx = cmd.index('-v')
        assert cmd[v_idx + 1] == f'{real_cwd}:{real_cwd}'

    def test_workdir_flag(self):
        c = ContainerConfig(image='test:latest')
        cmd = c.build_run_cmd('ls', cwd='/workspace')
        real_cwd = os.path.realpath('/workspace')
        assert '-w' in cmd
        w_idx = cmd.index('-w')
        assert cmd[w_idx + 1] == real_cwd

    def test_workdir_override(self):
        c = ContainerConfig(
            image='test:latest', workdir='/app'
        )
        cmd = c.build_run_cmd('ls', cwd='/workspace')
        w_idx = cmd.index('-w')
        assert cmd[w_idx + 1] == '/app'

    def test_setup_prepended(self):
        c = ContainerConfig(
            image='test:latest',
            setup='pip install -e .',
        )
        cmd = c.build_run_cmd('pytest tests/')
        assert cmd[-1] == 'pip install -e . && pytest tests/'

    def test_no_setup(self):
        c = ContainerConfig(image='test:latest')
        cmd = c.build_run_cmd('pytest tests/')
        assert cmd[-1] == 'pytest tests/'

    def test_env_vars(self):
        c = ContainerConfig(
            image='test:latest',
            env={'FOO': 'bar', 'BAZ': 'qux'},
        )
        cmd = c.build_run_cmd('echo test')
        # Find all -e flags
        env_pairs = []
        for i, arg in enumerate(cmd):
            if arg == '-e' and i + 1 < len(cmd):
                env_pairs.append(cmd[i + 1])
        assert 'FOO=bar' in env_pairs
        assert 'BAZ=qux' in env_pairs

    def test_extra_env(self):
        c = ContainerConfig(
            image='test:latest', env={'A': '1'}
        )
        cmd = c.build_run_cmd(
            'echo test', extra_env={'B': '2'}
        )
        env_pairs = []
        for i, arg in enumerate(cmd):
            if arg == '-e' and i + 1 < len(cmd):
                env_pairs.append(cmd[i + 1])
        assert 'A=1' in env_pairs
        assert 'B=2' in env_pairs

    def test_extra_volumes(self):
        c = ContainerConfig(image='test:latest')
        cmd = c.build_run_cmd(
            'ls',
            cwd='/workspace',
            extra_volumes={'/data': '/data'},
        )
        vol_pairs = []
        for i, arg in enumerate(cmd):
            if arg == '-v' and i + 1 < len(cmd):
                vol_pairs.append(cmd[i + 1])
        real_data = os.path.realpath('/data')
        assert f'{real_data}:/data' in vol_pairs

    def test_options_forwarded(self):
        c = ContainerConfig(
            image='test:latest',
            options=['--gpus', 'all', '--net=host'],
        )
        cmd = c.build_run_cmd('echo test')
        assert '--gpus' in cmd
        assert 'all' in cmd
        assert '--net=host' in cmd

    def test_podman_runtime(self):
        c = ContainerConfig(
            image='test:latest', runtime='podman'
        )
        cmd = c.build_run_cmd('echo test')
        assert cmd[0] == 'podman'

    def test_cwd_subpath_not_double_mounted(self):
        """If an explicit volume is a subpath of cwd,
        it should not be mounted separately."""
        c = ContainerConfig(
            image='test:latest',
            volumes={'/workspace/sub': '/workspace/sub'},
        )
        cmd = c.build_run_cmd('ls', cwd='/workspace')
        vol_pairs = []
        for i, arg in enumerate(cmd):
            if arg == '-v' and i + 1 < len(cmd):
                vol_pairs.append(cmd[i + 1])
        # Should only have 1 mount (the cwd), not the sub
        real_ws = os.path.realpath('/workspace')
        assert len(vol_pairs) == 1
        assert vol_pairs[0] == f'{real_ws}:{real_ws}'


class TestContainerConfigValidate:
    """Tests for ContainerConfig.validate()."""

    def test_validate_missing_runtime(self):
        c = ContainerConfig(
            image='test:latest',
            runtime='nonexistent_runtime_xyz',
        )
        with pytest.raises(RuntimeError, match="not found"):
            c.validate()
