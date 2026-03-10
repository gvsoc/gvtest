"""
Tests for runner.Target — target configuration, properties, env vars.
"""

import json
import os
import pytest
from gvtest.runner import Target


class TestTargetBasic:
    """Basic target creation and naming."""

    def test_default_config(self):
        t = Target('rv64')
        assert t.get_name() == 'rv64'
        assert t.config == {}

    def test_name(self):
        t = Target('pulp-open')
        assert t.name == 'pulp-open'
        assert t.get_name() == 'pulp-open'

    def test_json_config(self):
        config = json.dumps({'properties': {'chip': 'rv64'}})
        t = Target('rv64', config)
        assert t.config == {'properties': {'chip': 'rv64'}}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            Target('bad', 'not json')


class TestTargetSourceme:
    """Tests for target sourceme resolution."""

    def test_no_sourceme(self):
        t = Target('rv64')
        assert t.get_sourceme() is None

    def test_with_sourceme(self):
        config = json.dumps({'sourceme': 'my_env.sh'})
        t = Target('rv64', config)
        assert t.get_sourceme() == 'my_env.sh'

    def test_sourceme_env_expansion(self, monkeypatch):
        """${VAR} in sourceme should expand from environment."""
        monkeypatch.setenv('SDK_HOME', '/opt/sdk')
        config = json.dumps({
            'sourceme': '${SDK_HOME}/configs/setup.sh'
        })
        t = Target('rv64', config)
        assert t.get_sourceme() == '/opt/sdk/configs/setup.sh'

    def test_sourceme_missing_env_expands_empty(self, monkeypatch):
        """Missing env vars expand to empty string."""
        monkeypatch.delenv('NONEXISTENT_VAR_XYZ', raising=False)
        config = json.dumps({
            'sourceme': '${NONEXISTENT_VAR_XYZ}/file.sh'
        })
        t = Target('rv64', config)
        assert t.get_sourceme() == '/file.sh'


class TestTargetEnvvars:
    """Tests for target environment variable resolution."""

    def test_no_envvars(self):
        t = Target('rv64')
        assert t.get_envvars() is None

    def test_with_envvars_plain(self):
        """Plain string values without ${} pass through."""
        config = json.dumps({'envvars': {'MY_VAR': 'hello'}})
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'MY_VAR': 'hello'}

    def test_with_envvars_env_expansion(self, monkeypatch):
        """${VAR} in envvar values should expand."""
        monkeypatch.setenv('GCC_PATH', '/usr/local/gcc')
        config = json.dumps({
            'envvars': {'TOOLCHAIN': '${GCC_PATH}'}
        })
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'TOOLCHAIN': '/usr/local/gcc'}

    def test_envvars_multiple_expansions(self, monkeypatch):
        """Multiple ${VAR} refs in one value."""
        monkeypatch.setenv('BASE', '/opt')
        monkeypatch.setenv('VER', '2.0')
        config = json.dumps({
            'envvars': {'PATH': '${BASE}/tools/${VER}/bin'}
        })
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'PATH': '/opt/tools/2.0/bin'}

    def test_envvars_missing_env_expands_empty(self, monkeypatch):
        """Missing env vars expand to empty string."""
        monkeypatch.delenv('MISSING_VAR_ABC', raising=False)
        config = json.dumps({
            'envvars': {'X': '${MISSING_VAR_ABC}'}
        })
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'X': ''}

    def test_envvars_no_code_execution(self, tmp_path):
        """Code in envvar values is NOT executed."""
        marker = tmp_path / 'pwned.txt'
        config = json.dumps({
            'envvars': {
                'EXPLOIT': f'__import__("os").system("touch {marker}")'
            }
        })
        t = Target('rv64', config)
        envvars = t.get_envvars()
        # The value passes through as a literal string
        assert '__import__' in envvars['EXPLOIT']
        # And no file was created (code was not executed)
        assert not marker.exists()

    def test_envvars_path_join_pattern(self, monkeypatch):
        """Real-world pattern: path building with ${VAR}."""
        monkeypatch.setenv('PULP_SDK_HOME', '/home/user/sdk')
        config = json.dumps({
            'sourceme': '${PULP_SDK_HOME}/configs/pulp-open.sh',
            'envvars': {
                'TOOLCHAIN': '${PULP_SDK_HOME}/tools/gcc'
            }
        })
        t = Target('rv64', config)
        assert t.get_sourceme() == '/home/user/sdk/configs/pulp-open.sh'
        assert t.get_envvars() == {
            'TOOLCHAIN': '/home/user/sdk/tools/gcc'
        }


class TestTargetProperties:
    """Tests for target property formatting."""

    def test_format_no_properties(self):
        t = Target('rv64')
        assert t.format_properties('hello {name}') == 'hello {name}'

    def test_format_with_properties(self):
        config = json.dumps({
            'properties': {'name': 'world', 'ver': '2'}
        })
        t = Target('rv64', config)
        result = t.format_properties('hello {name} v{ver}')
        assert result == 'hello world v2'

    def test_get_property_exists(self):
        config = json.dumps({'properties': {'chip': 'rv64'}})
        t = Target('rv64', config)
        assert t.get_property('chip') == 'rv64'

    def test_get_property_missing(self):
        config = json.dumps({'properties': {'chip': 'rv64'}})
        t = Target('rv64', config)
        assert t.get_property('nonexistent') is None

    def test_get_property_no_properties(self):
        t = Target('rv64')
        assert t.get_property('anything') is None
