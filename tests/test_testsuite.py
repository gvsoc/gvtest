"""
Tests for gvtest.testsuite — the public API / abstract base classes and command types.
"""

import pytest
from gvtest.testsuite import Shell, Call, Checker, Command


class TestShellCommand:
    """Tests for the Shell command type."""

    def test_basic_creation(self):
        cmd = Shell('build', 'make build')
        assert cmd.name == 'build'
        assert cmd.cmd == 'make build'
        assert cmd.retval == 0

    def test_custom_retval(self):
        cmd = Shell('expected_fail', 'false', retval=1)
        assert cmd.retval == 1

    def test_negative_retval(self):
        cmd = Shell('signal', 'kill_self', retval=-9)
        assert cmd.retval == -9

    def test_is_command(self):
        cmd = Shell('x', 'echo hi')
        assert isinstance(cmd, Command)

    def test_complex_command(self):
        cmd = Shell('run', 'gvsoc --target=rv64 --work rv64 --binary /path/to/bin run')
        assert '--target=rv64' in cmd.cmd


class TestCallCommand:
    """Tests for the Call command type."""

    def test_basic_creation(self):
        def my_func():
            return 0
        cmd = Call('step', my_func)
        assert cmd.name == 'step'
        assert cmd.callback is my_func

    def test_lambda_callback(self):
        cmd = Call('check', lambda: 42)
        assert cmd.callback() == 42

    def test_is_command(self):
        cmd = Call('x', lambda: None)
        assert isinstance(cmd, Command)


class TestCheckerCommand:
    """Tests for the Checker command type."""

    def test_basic_creation(self):
        def checker(run, output):
            return (True, None)
        cmd = Checker('validate', checker)
        assert cmd.name == 'validate'
        # Checker stores (callback, args, kwargs) tuple
        assert cmd.callback[0] is checker
        assert cmd.callback[1] == ()
        assert cmd.callback[2] == {}

    def test_with_args(self):
        def checker(run, output, expected):
            return (expected in output, None)
        cmd = Checker('check', checker, 'hello')
        assert cmd.callback[1] == ('hello',)

    def test_with_kwargs(self):
        def checker(run, output, strict=False):
            return (True, None)
        cmd = Checker('check', checker, strict=True)
        assert cmd.callback[2] == {'strict': True}

    def test_with_args_and_kwargs(self):
        def checker(run, output, pattern, strict=False):
            return (True, None)
        cmd = Checker('check', checker, r'.*OK.*', strict=True)
        assert cmd.callback[1] == (r'.*OK.*',)
        assert cmd.callback[2] == {'strict': True}

    def test_is_command(self):
        cmd = Checker('x', lambda r, o: (True, None))
        assert isinstance(cmd, Command)
