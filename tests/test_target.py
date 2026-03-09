"""
Tests for runner.Target — target configuration, properties, env vars.
"""

import json
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
        config = json.dumps({'sourceme': "'my_env.sh'"})
        t = Target('rv64', config)
        assert t.get_sourceme() == 'my_env.sh'


class TestTargetEnvvars:
    """Tests for target environment variable resolution."""

    def test_no_envvars(self):
        t = Target('rv64')
        assert t.get_envvars() is None

    def test_with_envvars(self):
        config = json.dumps({'envvars': {'MY_VAR': "'hello'"}})
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'MY_VAR': 'hello'}

    def test_envvars_eval_failure(self):
        """Env vars that fail eval should get empty string."""
        config = json.dumps({'envvars': {'BAD': 'undefined_variable'}})
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'BAD': ''}

    def test_envvars_none_becomes_empty(self):
        """Env vars that eval to None should become empty string."""
        config = json.dumps({'envvars': {'NONE_VAR': 'None'}})
        t = Target('rv64', config)
        envvars = t.get_envvars()
        assert envvars == {'NONE_VAR': ''}

    def test_envvars_no_code_execution(self):
        """Env vars should use literal_eval, not eval — no arbitrary code."""
        config = json.dumps({'envvars': {'EXPLOIT': '__import__("os").system("echo pwned")'}})
        t = Target('rv64', config)
        envvars = t.get_envvars()
        # Should fail safely (literal_eval rejects function calls), not execute code
        assert envvars == {'EXPLOIT': ''}


class TestTargetProperties:
    """Tests for target property formatting."""

    def test_format_no_properties(self):
        t = Target('rv64')
        assert t.format_properties('hello {name}') == 'hello {name}'

    def test_format_with_properties(self):
        config = json.dumps({'properties': {'name': 'world', 'ver': '2'}})
        t = Target('rv64', config)
        assert t.format_properties('hello {name} v{ver}') == 'hello world v2'

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
