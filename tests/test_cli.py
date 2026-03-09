"""
Tests for gvtest CLI (__main__.py) — end-to-end command-line invocation.
"""

import os
import sys
import subprocess
import pytest
from pathlib import Path


GVTEST_DIR = os.path.join(os.path.dirname(__file__), '..')
PYTHON_DIR = os.path.join(GVTEST_DIR, 'python')


def run_gvtest(*args, cwd=None, env=None):
    """Run gvtest as a subprocess and return result."""
    cmd_env = os.environ.copy()
    cmd_env['PYTHONPATH'] = PYTHON_DIR + ':' + cmd_env.get('PYTHONPATH', '')
    if env:
        cmd_env.update(env)
    
    result = subprocess.run(
        [sys.executable, '-m', 'gvtest'] + list(args),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=cmd_env,
        timeout=30,
    )
    return result


class TestCLIBasic:
    """Basic CLI invocation tests."""

    def test_default_commands_run(self, tmp_path):
        """Default (no command) should run: run, table, summary."""
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cli')
    test = testset.new_test('hello')
    test.add_command(Shell('run', 'echo hi'))
''')
        result = run_gvtest('--testset', str(testset), '--threads', '1')
        assert result.returncode == 0
        assert 'All tests passed' in result.stdout or 'Test Summary' in result.stdout

    def test_run_command(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cli')
    test = testset.new_test('hello')
    test.add_command(Shell('run', 'echo hi'))
''')
        result = run_gvtest('run', '--testset', str(testset), '--threads', '1')
        assert result.returncode == 0

    def test_tests_command(self, tmp_path):
        """'tests' command should list tests without running them."""
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('listing')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', 'echo a'))
    test = testset.new_test('test_b')
    test.add_command(Shell('run', 'echo b'))
''')
        result = run_gvtest('tests', '--testset', str(testset), '--threads', '1')
        assert result.returncode == 0
        assert 'test_a' in result.stdout
        assert 'test_b' in result.stdout

    def test_invalid_command(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('x')
''')
        result = run_gvtest('nonexistent_cmd', '--testset', str(testset), '--threads', '1')
        assert result.returncode != 0
        assert 'Invalid command' in result.stderr


class TestCLIFiltering:
    """CLI test filtering options."""

    def test_select_test(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('filter')
    t1 = testset.new_test('run_me')
    t1.add_command(Shell('run', 'echo yes'))
    t2 = testset.new_test('skip_me')
    t2.add_command(Shell('run', 'exit 1'))
''')
        result = run_gvtest(
            'run', 'summary',
            '--testset', str(testset),
            '--test', 'filter:run_me',
            '--threads', '1'
        )
        assert result.returncode == 0
        # Only run_me should run and pass
        assert 'Passed' in result.stdout

    def test_skip_test(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('filter')
    t1 = testset.new_test('keep')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('skip_me')
    t2.add_command(Shell('run', 'exit 1'))
''')
        result = run_gvtest(
            'run', 'summary',
            '--testset', str(testset),
            '--skip', 'filter:skip_me',
            '--threads', '1'
        )
        assert result.returncode == 0


class TestCLINoFail:
    """Tests for --no-fail flag."""

    def test_no_fail_exits_on_failure(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('nofail')
    test = testset.new_test('failing')
    test.add_command(Shell('run', 'exit 1'))
''')
        result = run_gvtest(
            '--testset', str(testset),
            '--threads', '1',
            '--no-fail'
        )
        assert result.returncode != 0

    def test_no_fail_passes_on_success(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('nofail')
    test = testset.new_test('passing')
    test.add_command(Shell('run', 'echo ok'))
''')
        result = run_gvtest(
            '--testset', str(testset),
            '--threads', '1',
            '--no-fail'
        )
        assert result.returncode == 0


class TestCLIJunit:
    """Tests for JUnit report generation via CLI."""

    def test_junit_command(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('junit_cli')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', 'echo ok'))
''')
        report_path = tmp_path / 'reports'
        result = run_gvtest(
            'all',
            '--testset', str(testset),
            '--threads', '1',
            '--junit-report-path', str(report_path)
        )
        assert result.returncode == 0
        assert report_path.exists()
        xml_files = list(report_path.glob('*.xml'))
        assert len(xml_files) >= 1


class TestCLIMissingTestset:
    """Tests for error handling with missing testset."""

    def test_default_testset_missing(self, tmp_path):
        """When no --testset given and no testset.cfg in cwd, should error."""
        result = run_gvtest('--threads', '1', cwd=str(tmp_path))
        assert result.returncode != 0


class TestCLIAllCommand:
    """Tests for the 'all' command (run + table + summary + junit)."""

    def test_all_command(self, tmp_path):
        testset = tmp_path / 'testset.cfg'
        testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('all_cmd')
    test = testset.new_test('basic')
    test.add_command(Shell('run', 'echo ok'))
''')
        report_path = tmp_path / 'junit-reports'
        result = run_gvtest(
            'all',
            '--testset', str(testset),
            '--threads', '1',
            '--junit-report-path', str(report_path)
        )
        assert result.returncode == 0
        assert 'Test Summary' in result.stdout
        assert report_path.exists()
