"""
Tests for gvtest.runner — Runner, TestRun, test execution, stats, filtering, reporting.
"""

import os
import sys
import json
import pytest
import tempfile
import threading
from pathlib import Path
from io import StringIO
from unittest.mock import patch, MagicMock

from gvtest.runner import (
    Runner, TestRun, TestImpl, TestsetImpl, TestCommon,
    MakeTestImpl, Target, Worker,
    TestRunStats, TestStats, TestsetStats,
    table_dump_row,
)
from gvtest.testsuite import Shell, Call, Checker


# ---------------------------------------------------------------------------
# Runner initialization
# ---------------------------------------------------------------------------

class TestRunnerInit:
    """Tests for Runner construction and defaults."""

    def test_default_config(self):
        r = Runner(properties=[], flags=[])
        assert r.config == 'default'
        assert r.platform == 'gvsoc'
        assert r.load_average == 0.9
        assert r.max_timeout == -1
        assert r.stdout is False
        assert r.safe_stdout is False
        assert r.report_all is False

    def test_custom_config(self):
        r = Runner(config='debug', properties=[], flags=[], platform='rtl')
        assert r.config == 'debug'
        assert r.platform == 'rtl'

    def test_properties_parsing(self):
        r = Runner(properties=['arch=rv64', 'mode=sim'], flags=[])
        assert r.get_property('arch') == 'rv64'
        assert r.get_property('mode') == 'sim'
        assert r.get_property('nonexistent') is None

    def test_default_target_no_targets(self):
        r = Runner(properties=[], flags=[])
        assert r.target_names == ['default']
        assert r.default_target.name == 'default'

    def test_explicit_targets(self):
        r = Runner(properties=[], flags=[], targets=['rv64', 'pulp-open'])
        assert r.target_names == ['rv64', 'pulp-open']
        assert r.default_target.name == 'rv64'

    def test_get_platform(self):
        r = Runner(properties=[], flags=[], platform='fpga')
        assert r.get_platform() == 'fpga'


# ---------------------------------------------------------------------------
# Test selection and skipping
# ---------------------------------------------------------------------------

class TestFiltering:
    """Tests for test selection and skip logic."""

    def test_all_selected_when_no_filter(self):
        r = Runner(properties=[], flags=[])
        # Create a mock test
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_selected_by_prefix(self):
        r = Runner(properties=[], flags=[], test_list=['suite:test_a'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_not_selected(self):
        r = Runner(properties=[], flags=[], test_list=['suite:test_b'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is False

    def test_selected_by_partial_prefix(self):
        """Test list uses prefix matching."""
        r = Runner(properties=[], flags=[], test_list=['suite'])
        mock_test = MagicMock()
        mock_test.get_full_name.return_value = 'suite:test_a'
        assert r.is_selected(mock_test) is True

    def test_skip_by_prefix(self):
        r = Runner(properties=[], flags=[], test_skip_list=['suite:skip_me'])
        assert r.is_skipped('suite:skip_me') is True
        assert r.is_skipped('suite:skip_me:subtest') is True
        assert r.is_skipped('suite:keep_me') is False

    def test_no_skip_list(self):
        r = Runner(properties=[], flags=[])
        assert r.is_skipped('anything') is False

    def test_multiple_skip_entries(self):
        r = Runner(properties=[], flags=[], test_skip_list=['a', 'b'])
        assert r.is_skipped('a:test') is True
        assert r.is_skipped('b:test') is True
        assert r.is_skipped('c:test') is False


# ---------------------------------------------------------------------------
# Test name tracking
# ---------------------------------------------------------------------------

class TestNameTracking:
    """Tests for max test name length tracking."""

    def test_declare_name_tracks_max(self):
        r = Runner(properties=[], flags=[])
        r.declare_name('short')
        assert r.get_max_testname_len() == 5
        r.declare_name('much_longer_name')
        assert r.get_max_testname_len() == 16
        r.declare_name('tiny')
        assert r.get_max_testname_len() == 16  # Still the max


# ---------------------------------------------------------------------------
# Testset loading and import
# ---------------------------------------------------------------------------

class TestTestsetImport:
    """Tests for loading testset.cfg files."""

    def test_load_simple_testset(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('basic')
    test = testset.new_test('echo_test')
    test.add_command(Shell('run', 'echo hello'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        assert len(r.testsets) == 1
        assert r.testsets[0].name == 'basic'
        assert len(r.testsets[0].tests) == 1
        assert r.testsets[0].tests[0].name == 'echo_test'

    def test_load_nonexistent_testset(self, tmp_path):
        r = Runner(properties=[], flags=[], nb_threads=1)
        with pytest.raises(RuntimeError, match='Unable to open'):
            r.add_testset(str(tmp_path / 'nonexistent.cfg'))

    def test_nested_testsets(self, tmp_path):
        sub_dir = tmp_path / 'sub'
        sub_dir.mkdir()
        
        sub_testset = sub_dir / 'testset.cfg'
        sub_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('sub')
    test = testset.new_test('sub_test')
    test.add_command(Shell('run', 'echo sub'))
''')
        
        main_testset = tmp_path / 'testset.cfg'
        main_testset.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('main')
    testset.import_testset(file='sub/testset.cfg')
''')
        
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(main_testset))
        assert r.testsets[0].name == 'main'
        assert len(r.testsets[0].testsets) == 1
        assert r.testsets[0].testsets[0].name == 'sub'


# ---------------------------------------------------------------------------
# Test execution (end-to-end with real shell commands)
# ---------------------------------------------------------------------------

class TestExecution:
    """End-to-end tests running actual shell commands."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        """Helper to create, load, and run a testset."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_passing_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('pass_suite')
    test = testset.new_test('pass_test')
    test.add_command(Shell('run', 'echo hello'))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 0

    def test_failing_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('fail_suite')
    test = testset.new_test('fail_test')
    test.add_command(Shell('run', 'exit 1'))
''')
        assert r.stats.stats['failed'] == 1
        assert r.stats.stats['passed'] == 0

    def test_expected_nonzero_retval(self, tmp_path):
        """Shell command with expected non-zero retval should pass."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('retval_suite')
    test = testset.new_test('expected_fail')
    test.add_command(Shell('run', 'exit 1', retval=1))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 0

    def test_multiple_commands_stop_on_failure(self, tmp_path):
        """If a command fails, subsequent commands should not run."""
        marker = tmp_path / 'marker.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('multi')
    test = testset.new_test('stop_on_fail')
    test.add_command(Shell('step1', 'exit 1'))
    test.add_command(Shell('step2', 'touch {marker}'))
''')
        assert r.stats.stats['failed'] == 1
        assert not marker.exists()  # step2 should not have run

    def test_multiple_tests(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('multi')
    for i in range(5):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'echo ok'))
''')
        assert r.stats.stats['passed'] == 5
        assert r.stats.stats['failed'] == 0

    def test_mixed_pass_fail(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('mixed')
    t1 = testset.new_test('pass')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('fail')
    t2.add_command(Shell('run', 'exit 1'))
''')
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['failed'] == 1

    def test_test_output_captured(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('output')
    test = testset.new_test('echo_test')
    test.add_command(Shell('run', 'echo MAGIC_OUTPUT_STRING'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        test = r.testsets[0].tests[0]
        run = test.runs[0]
        assert 'MAGIC_OUTPUT_STRING' in run.output

    def test_checker_command_pass(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def my_checker(run, output):
    if 'SUCCESS' in output:
        return (True, None)
    return (False, "SUCCESS not found")

def testset_build(testset):
    testset.set_name('checker')
    test = testset.new_test('check_test')
    test.add_command(Shell('run', 'echo SUCCESS'))
    test.add_command(Checker('validate', my_checker))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_checker_command_fail(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def my_checker(run, output):
    if 'SUCCESS' in output:
        return (True, None)
    return (False, "SUCCESS not found")

def testset_build(testset):
    testset.set_name('checker')
    test = testset.new_test('check_fail')
    test.add_command(Shell('run', 'echo FAILURE'))
    test.add_command(Checker('validate', my_checker))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['failed'] == 1

    def test_call_command(self, tmp_path):
        marker = tmp_path / 'call_marker.txt'
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(f'''
from gvtest.testsuite import *

def my_callback():
    with open("{marker}", "w") as f:
        f.write("called")
    return 0

def testset_build(testset):
    testset.set_name('call')
    test = testset.new_test('call_test')
    test.add_command(Call('step', my_callback))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert marker.exists()
        assert marker.read_text() == 'called'

    def test_skipped_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('skip')
    test = testset.new_test('skipped')
    test.skip('not ready')
    test.add_command(Shell('run', 'echo should not run'))
''')
        assert r.stats.stats['skipped'] == 1
        assert r.stats.stats['passed'] == 0

    def test_skip_from_command_line(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    t1 = testset.new_test('keep')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('skip_me')
    t2.add_command(Shell('run', 'echo should not run'))
''', test_skip_list=['suite:skip_me'])
        assert r.stats.stats['passed'] == 1
        assert r.stats.stats['skipped'] == 1

    def test_select_specific_test(self, tmp_path):
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    t1 = testset.new_test('run_me')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('not_me')
    t2.add_command(Shell('run', 'echo should not run'))
''', test_list=['suite:run_me'])
        # Only run_me should be in the testset (not_me filtered at creation)
        assert r.stats.stats['passed'] == 1
        total = r.stats.stats['passed'] + r.stats.stats['failed']
        assert total == 1


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

class TestTimeout:
    """Tests for test timeout functionality."""

    def test_timeout_kills_test(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('timeout')
    test = testset.new_test('slow_test')
    test.add_command(Shell('run', 'sleep 60'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1, max_timeout=2)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['failed'] == 1
        run = r.testsets[0].tests[0].runs[0]
        assert 'Timeout reached' in run.output


# ---------------------------------------------------------------------------
# Benchmark extraction
# ---------------------------------------------------------------------------

class TestBenchmarks:
    """Tests for benchmark result extraction."""

    def test_bench_extraction(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo "Cycles: 42"'))
    test.add_bench(r'Cycles: (\\d+)', 'cycles', 'CPU cycles')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert 'cycles' in r.bench_results
        assert r.bench_results['cycles'][0] == 42.0

    def test_bench_csv_export(self, tmp_path):
        csv_file = tmp_path / 'bench.csv'
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('bench')
    test = testset.new_test('perf')
    test.add_command(Shell('run', 'echo "Cycles: 100"'))
    test.add_bench(r'Cycles: (\\d+)', 'cycles', 'CPU cycles')
''')
        r = Runner(properties=[], flags=[], nb_threads=1, bench_csv_file=str(csv_file))
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert csv_file.exists()
        content = csv_file.read_text()
        assert 'cycles' in content


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

class TestStats:
    """Tests for statistics collection and aggregation."""

    def _make_mock_run(self, status='passed', duration=1.0, target=None, name='test'):
        """Create a mock test run with given status."""
        run = MagicMock()
        run.status = status
        run.duration = duration
        run.target = target
        run.config = 'default'
        run.test = MagicMock()
        run.test.get_full_name.return_value = name
        run.test.name = name
        run.get_target_name.return_value = 'default'
        run.get_stats = lambda stats: self._apply_stats(stats, status, duration)
        return run

    def _apply_stats(self, stats, status, duration):
        stats[status] += 1
        stats['duration'] = duration

    def test_run_stats_passed(self):
        run = self._make_mock_run('passed', 1.5)
        stats = TestRunStats(run)
        assert stats.stats['passed'] == 1
        assert stats.stats['failed'] == 0
        assert stats.stats['duration'] == 1.5

    def test_run_stats_failed(self):
        run = self._make_mock_run('failed', 0.5)
        stats = TestRunStats(run)
        assert stats.stats['failed'] == 1
        assert stats.stats['passed'] == 0

    def test_stats_propagate_to_parent(self):
        from gvtest.runner import TestStats as RealTestStats
        parent = RealTestStats()
        run = self._make_mock_run('passed', 1.0)
        TestRunStats(run, parent=parent)
        assert parent.stats['passed'] == 1


# ---------------------------------------------------------------------------
# JUnit report
# ---------------------------------------------------------------------------

class TestJunitReport:
    """Tests for JUnit XML report generation."""

    def test_junit_output(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('junit_suite')
    t1 = testset.new_test('pass_test')
    t1.add_command(Shell('run', 'echo ok'))
    t2 = testset.new_test('fail_test')
    t2.add_command(Shell('run', 'exit 1'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        report_path = tmp_path / 'junit-reports'
        r.dump_junit(str(report_path))
        
        assert report_path.exists()
        xml_files = list(report_path.glob('*.xml'))
        assert len(xml_files) >= 1
        
        content = xml_files[0].read_text()
        assert '<?xml version="1.0"' in content
        assert 'testsuite' in content
        assert 'testcase' in content
        assert 'pass_test' in content
        assert 'fail_test' in content
        assert '<failure>' in content

    def test_junit_skipped_test(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('junit_skip')
    test = testset.new_test('skipped')
    test.skip('not implemented')
    test.add_command(Shell('run', 'echo nope'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        report_path = tmp_path / 'junit-reports'
        r.dump_junit(str(report_path))
        
        xml_files = list(report_path.glob('*.xml'))
        content = xml_files[0].read_text()
        assert '<skipped' in content


# ---------------------------------------------------------------------------
# Testset hierarchy and naming
# ---------------------------------------------------------------------------

class TestTestsetHierarchy:
    """Tests for testset naming and nesting."""

    def test_full_name_single_level(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        assert r.testsets[0].tests[0].get_full_name() == 'suite:test_a'

    def test_full_name_nested(self, tmp_path):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def sub_build(testset):
    testset.set_name('sub')
    test = testset.new_test('deep')
    test.add_command(Shell('run', 'echo ok'))

def testset_build(testset):
    testset.set_name('top')
    child = testset.new_testset('sub')
    sub_build(child)
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        # Navigate: top -> sub testset -> deep test
        sub = r.testsets[0].testsets[0]
        assert sub.get_full_name() == 'top:sub'
        assert sub.tests[0].get_full_name() == 'top:sub:deep'


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

class TestParallelExecution:
    """Tests for multi-threaded test execution."""

    def test_parallel_tests(self, tmp_path):
        """Multiple tests should all complete with parallel workers."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('parallel')
    for i in range(10):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'echo ok'))
''')
        r = Runner(properties=[], flags=[], nb_threads=4)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 10
        assert r.stats.stats['failed'] == 0


# ---------------------------------------------------------------------------
# Environment and working directory
# ---------------------------------------------------------------------------

class TestEnvironment:
    """Tests for test working directory and environment."""

    def test_working_directory(self, tmp_path):
        """Test runs in the testset's directory."""
        subdir = tmp_path / 'workdir'
        subdir.mkdir()
        testset_file = subdir / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cwd')
    test = testset.new_test('pwd_test')
    test.add_command(Shell('run', 'pwd > cwd_output.txt'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        
        output = (subdir / 'cwd_output.txt').read_text().strip()
        assert output == str(subdir)


# ---------------------------------------------------------------------------
# Targets with testsets
# ---------------------------------------------------------------------------

class TestTargetsInTestset:
    """Tests for target-aware testset execution."""

    def test_testset_with_target(self, tmp_path):
        # Define target in gvtest.yaml
        config = tmp_path / 'gvtest.yaml'
        config.write_text('targets:\n  my_target: {}\n')

        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def target_tests(testset):
    test = testset.new_test('hello')
    test.add_command(Shell('run', 'echo ok'))

def testset_build(testset):
    testset.set_name('targeted')
    testset.add_testset(callback=target_tests)
''')
        r = Runner(properties=[], flags=[], nb_threads=1, targets=['my_target'])
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1


# ---------------------------------------------------------------------------
# Config integration (gvtest.yaml + testset loading)
# ---------------------------------------------------------------------------

class TestConfigIntegration:
    """Tests for gvtest.yaml python_paths integration during testset loading."""

    def test_python_paths_available_during_load(self, tmp_path):
        """Modules from gvtest.yaml python_paths should be importable during testset_build."""
        # Create a Python package to import
        lib_dir = tmp_path / 'mylib'
        lib_dir.mkdir()
        (lib_dir / '__init__.py').write_text('')
        (lib_dir / 'helpers.py').write_text('MAGIC = 42\n')
        
        # Create gvtest.yaml pointing to the lib
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir.parent}\n')
        
        # Create testset that imports from the lib
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
from mylib.helpers import MAGIC

def testset_build(testset):
    testset.set_name('config_test')
    test = testset.new_test('import_test')
    test.add_command(Shell('run', f'echo {MAGIC}'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_python_paths_available_during_testset_build(self, tmp_path):
        """Imports inside testset_build() should work (paths still in sys.path)."""
        lib_dir = tmp_path / 'buildlib'
        lib_dir.mkdir()
        (lib_dir / '__init__.py').write_text('')
        (lib_dir / 'tool.py').write_text('CMD = "echo from_buildlib"\n')
        
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir.parent}\n')
        
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    # This import happens inside testset_build — paths must still be available
    from buildlib.tool import CMD
    testset.set_name('build_import')
    test = testset.new_test('test')
    test.add_command(Shell('run', CMD))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 1

    def test_python_paths_isolated_between_testsets(self, tmp_path):
        """After loading a testset, its python_paths should be removed from sys.path."""
        lib_dir = tmp_path / 'isolated_lib'
        lib_dir.mkdir()
        
        config = tmp_path / 'gvtest.yaml'
        config.write_text(f'python_paths:\n  - {lib_dir}\n')
        
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('isolation')
    test = testset.new_test('test')
    test.add_command(Shell('run', 'echo ok'))
''')
        
        original_path = sys.path.copy()
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        
        # The isolated_lib should NOT be in sys.path after loading
        assert str(lib_dir) not in sys.path


# ---------------------------------------------------------------------------
# Module name collision
# ---------------------------------------------------------------------------

class TestModuleIsolation:
    """Tests for unique module naming during testset import."""

    def test_two_testsets_dont_collide(self, tmp_path):
        """Two different testset files should not overwrite each other's modules."""
        dir_a = tmp_path / 'a'
        dir_a.mkdir()
        (dir_a / 'testset.cfg').write_text('''
from gvtest.testsuite import *
MARKER_A = "from_a"

def testset_build(testset):
    testset.set_name('suite_a')
    test = testset.new_test('test_a')
    test.add_command(Shell('run', f'echo {MARKER_A}'))
''')
        
        dir_b = tmp_path / 'b'
        dir_b.mkdir()
        (dir_b / 'testset.cfg').write_text('''
from gvtest.testsuite import *
MARKER_B = "from_b"

def testset_build(testset):
    testset.set_name('suite_b')
    test = testset.new_test('test_b')
    test.add_command(Shell('run', f'echo {MARKER_B}'))
''')
        
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(dir_a / 'testset.cfg'))
        r.add_testset(str(dir_b / 'testset.cfg'))
        r.start()
        r.run()
        r.stop()
        assert r.stats.stats['passed'] == 2
        assert r.stats.stats['failed'] == 0
        # Verify each test got the right output
        run_a = r.testsets[0].tests[0].runs[0]
        run_b = r.testsets[1].tests[0].runs[0]
        assert 'from_a' in run_a.output
        assert 'from_b' in run_b.output


# ---------------------------------------------------------------------------
# Max output length
# ---------------------------------------------------------------------------

class TestMaxOutputLen:
    """Tests for --max-output-len enforcement."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_output_truncated(self, tmp_path):
        """Output beyond max_output_len should be truncated."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('trunc')
    test = testset.new_test('big_output')
    test.add_command(Shell('run', 'seq 1 10000'))
''', max_output_len=200)
        run = r.testsets[0].tests[0].runs[0]
        # Output should contain truncation notice
        assert 'truncated' in run.output.lower() or 'Truncated' in run.output

    def test_no_truncation_by_default(self, tmp_path):
        """Without max_output_len, output is not truncated."""
        r = self._run_testset(tmp_path, '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('notrunc')
    test = testset.new_test('output')
    test.add_command(Shell('run', 'seq 1 100'))
''')
        run = r.testsets[0].tests[0].runs[0]
        assert 'truncated' not in run.output.lower()
        assert '100' in run.output


# ---------------------------------------------------------------------------
# Command filtering (--cmd / --cmd-exclude)
# ---------------------------------------------------------------------------

class TestCommandFiltering:
    """Tests for --cmd and --cmd-exclude options."""

    def _run_testset(self, tmp_path, testset_content, **runner_kwargs):
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(testset_content)
        defaults = {'properties': [], 'flags': [], 'nb_threads': 1}
        defaults.update(runner_kwargs)
        r = Runner(**defaults)
        r.add_testset(str(testset_file))
        r.start()
        r.run()
        r.stop()
        return r

    def test_cmd_filter_runs_only_selected(self, tmp_path):
        """--cmd should run only the named commands."""
        marker = tmp_path / 'step2_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cmdfilter')
    test = testset.new_test('test')
    test.add_command(Shell('step1', 'echo step1'))
    test.add_command(Shell('step2', 'touch {marker}'))
''', commands=['step1'])
        # step2 should NOT have run
        assert not marker.exists()

    def test_cmd_exclude_skips_command(self, tmp_path):
        """--cmd-exclude should skip the named commands."""
        marker = tmp_path / 'clean_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('cmdexclude')
    test = testset.new_test('test')
    test.add_command(Shell('clean', 'touch {marker}'))
    test.add_command(Shell('run', 'echo ok'))
''', commands_exclude=['clean'])
        # clean should NOT have run
        assert not marker.exists()
        assert r.stats.stats['passed'] == 1

    def test_no_cmd_filter_runs_all(self, tmp_path):
        """Without --cmd/--cmd-exclude, all commands run."""
        marker = tmp_path / 'all_ran.txt'
        r = self._run_testset(tmp_path, f'''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('allcmds')
    test = testset.new_test('test')
    test.add_command(Shell('step1', 'echo ok'))
    test.add_command(Shell('step2', 'touch {marker}'))
''')
        assert marker.exists()


# ---------------------------------------------------------------------------
# Graceful interrupt
# ---------------------------------------------------------------------------

class TestGracefulInterrupt:
    """Tests for signal handling."""

    def test_interrupted_flag_clears_pending(self, tmp_path):
        """Setting _interrupted should prevent pending tests from being dispatched."""
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text('''
from gvtest.testsuite import *
def testset_build(testset):
    testset.set_name('int')
    for i in range(5):
        test = testset.new_test(f'test_{i}')
        test.add_command(Shell('run', 'sleep 10'))
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(testset_file))
        r.start()
        
        # Mark as interrupted before running — check_pending_tests should exit early
        r._interrupted = True
        
        # Enqueue tests
        for testset in r.testsets:
            testset.enqueue()
        
        # Check that pending tests get cleared
        r.check_pending_tests()
        r.stop()
        
        # All pending tests should have been dropped
        assert len(r.pending_tests) == 0
        assert r.nb_pending_tests == 0
