#!/usr/bin/env python3

#
# Copyright (C) 2023 ETH Zurich, University of Bologna and GreenWaves Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Test runner — orchestration, worker threads, and CLI-facing API.

This module was split from a monolithic runner.py. The other pieces now live in:
  - targets.py      — Target class
  - tests.py        — TestRun, TestCommon, TestImpl, and specialized test types
  - stats.py        — TestRunStats, TestStats, TestsetStats
  - reporting.py    — bcolors, table_dump_row
  - testset_impl.py — TestsetImpl
  - config.py       — Hierarchical gvtest.yaml config loader
"""

from __future__ import annotations

import os
import logging
import signal
import sys
import csv
import queue
import threading
import time
import importlib
import importlib.util
from importlib.machinery import SourceFileLoader
from types import FrameType
from typing import Any

import psutil
import rich.table
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn
from rich.align import Align

from pathlib import Path
from gvtest.config import get_python_paths_for_dir, ConfigLoader
from gvtest.targets import Target
from gvtest.stats import TestsetStats
from gvtest.testset_impl import TestsetImpl

# Re-export classes that external code (tests, __main__) may import from runner
from gvtest.targets import Target
from gvtest.tests import (
    TestRun, TestCommon, TestImpl, MakeTestImpl,
    GvrunTestImpl, SdkTestImpl, NetlistPowerSdkTestImpl,
)
from gvtest.stats import TestRunStats, TestStats, TestsetStats
from gvtest.reporting import table_dump_row


class Worker(threading.Thread):

    def __init__(self, runner: Runner) -> None:
        super().__init__(daemon=True)

        self.runner: Runner = runner

    def run(self) -> None:
        while True:
            test: TestRun | None = self.runner.pop_test()
            if test is None:
                return
            test.run()


class Runner():

    def __init__(
            self, config: str = 'default',
            load_average: float = 0.9,
            nb_threads: int = 0,
            properties: list[str] | None = None,
            stdout: bool = False,
            safe_stdout: bool = False,
            max_output_len: int = -1,
            max_timeout: int = -1,
            test_list: list[str] | None = None,
            test_skip_list: list[str] | None = None,
            commands: list[str] | None = None,
            commands_exclude: list[str] | None = None,
            flags: list[str] | None = None,
            bench_csv_file: str | None = None,
            bench_regexp: str | None = None,
            targets: list[str] | None = None,
            platform: str = 'gvsoc',
            report_all: bool = False,
            progress: bool = False
    ) -> None:
        self.nb_threads: int = nb_threads
        self.queue: queue.Queue[TestRun | None] = queue.Queue()
        self.testsets: list[TestsetImpl] = []
        self.pending_tests: list[TestRun] = []
        self.active_runs: list[TestRun] = []
        self.max_testname_len: int = 0
        self.config: str = config
        self.event: threading.Event = threading.Event()
        self.lock: threading.Lock = threading.Lock()
        self.load_average: float = load_average
        self.stdout: bool = stdout
        self.safe_stdout: bool = safe_stdout
        self.nb_pending_tests: int = 0
        self.test_skip_list: list[str] | None = test_skip_list
        self.max_timeout: int = max_timeout
        self.max_output_len: int = max_output_len
        self.commands_filter: list[str] | None = commands
        self.commands_exclude: list[str] | None = commands_exclude
        self.flags: list[str] = flags if flags is not None else []
        self.bench_results: dict[str, list[Any]] = {}
        self.bench_csv_file: str | None = bench_csv_file
        self.properties: dict[str, str] = {}
        self.test_list: list[str] | None = test_list
        self.target_names: list[str] = targets if targets is not None else ['default']
        self.platform: str = platform
        if targets is None:
            self.default_target: Target = Target('default')
        else:
            self.default_target = Target(self.target_names[0])
        self.cpu_poll_interval: float = 0.1
        self.report_all: bool = report_all
        self.progress: bool = progress
        self.live_display: Any = None
        self.tui: Any = None
        self.stats: TestsetStats = TestsetStats()
        self.nb_total_tests: int = 0
        self._module_cache: dict[str, Any] = {}
        if properties is not None:
            for prop in properties:
              name, value = prop.split('=')
              self.properties[name] = value


        if bench_csv_file is not None:
            if os.path.exists(bench_csv_file):
                with open(bench_csv_file, 'r') as file:
                    csv_reader = csv.reader(file)
                    for row in csv_reader:
                        self.bench_results[row[0]] = row[1:]

    def get_active_targets(self) -> list[str]:
        return self.target_names

    def get_platform(self) -> str:
        return self.platform

    def get_property(self, name: str) -> str | None:
        return self.properties.get(name)

    def is_selected(self, test: TestCommon) -> bool:
        if self.test_list is None:
            return True

        for selected_test in self.test_list:
            full_name = test.get_full_name()
            if full_name is not None and full_name.find(selected_test) == 0:
                return True

        return False

    def is_skipped(self, name: str) -> bool:
        if self.test_skip_list is not None:
            for skip in self.test_skip_list:
                if name.find(skip) == 0:
                    return True

        return False

    def tests(self) -> None:
        table = rich.table.Table(title=f'tests', title_justify="left")
        table.add_column('Name')
        table.add_column('Path')
        table.add_column('Targets')

        for testset in self.testsets:
            testset.dump_tests(table)

        print()
        rich.print(table)

    def summary(self) -> None:
        failed: int | float = self.stats.stats['failed']
        passed: int | float = self.stats.stats['passed']
        skipped: int | float = self.stats.stats['skipped']
        excluded: int | float = self.stats.stats['excluded']
        total: int | float = failed + passed

        console = Console()
        table = Table(show_header=False)
        table.add_column("Status", justify="center")
        table.add_column("Count", justify="right")
        table.add_row("Test Summary", "", style="bold", end_section=True)
        table.add_row("Total", str(total))
        table.add_row("Passed", str(passed))
        table.add_row("Failed", str(failed))
        table.add_row("Skipped", str(skipped))
        table.add_row("Excluded", str(excluded))

        console.print(table)

        success_ratio: float = passed / total if total > 0 else 0
        percent: int = int(success_ratio * 100)

        if passed == total:
            msg: str = "[bold green]All tests passed[/bold green]"
        else:
            msg = f"[bold red]{passed}/{total} tests passed ({percent}%).[/bold red]"

        final_bar = Progress(
            BarColumn(bar_width=len(msg))
        )

        task = final_bar.add_task("", total=100, completed=percent)

        if passed == total:
            content: Any = msg
        else:
            content = Group(
                Align.center(msg, vertical="middle"),
                Align.center(final_bar, vertical="middle")
            )

        console.print(Panel.fit(
            content,
            border_style="green" if passed == total else "red",
            padding=(1, 2)
        ))

    def run(self) -> None:
        self.event.clear()
        self.nb_total_tests = 0

        # Start live display before enqueue so it catches
        # skipped/excluded tests too
        if self.progress and self.tui is None:
            from gvtest.live_display import LiveDisplay
            from rich.console import Console
            self.live_display = LiveDisplay(
                Console(highlight=False, stderr=True)
            )
            # Start with 0, update total after enqueue
            self.live_display.start(0)

        for testset in self.testsets:
            testset.enqueue()

        # Update totals now that all tests are counted
        if self.live_display is not None:
            self.live_display.set_total(
                self.nb_total_tests
            )

        # Notify TUI of total test count
        if self.tui is not None:
            self.tui.set_total(self.nb_total_tests)

        if len(self.pending_tests) > 0:
            self.check_pending_tests()

        # Wait if there are still tests running
        # (includes both regular and pytest batch tests)
        self.lock.acquire()
        should_wait: bool = self.nb_pending_tests > 0
        self.lock.release()
        if should_wait:
            # Use a timeout loop so SIGINT can be delivered
            # (event.wait() without timeout can block signal
            # handling on some platforms)
            while not self.event.is_set():
                self.event.wait(timeout=0.5)

        # Stop live display
        if self.live_display is not None:
            self.live_display.stop()
            self.live_display = None

        self.stats: TestsetStats = TestsetStats()
        for testset in self.testsets:
            self.stats.add_child_testset(testset)

        if self.bench_csv_file is not None:
            with open(self.bench_csv_file, 'w') as file:
                csv_writer = csv.writer(file)
                for key, value in self.bench_results.items():
                    csv_writer.writerow([key] + value)



    def declare_name(self, name: str) -> None:
        name_len: int = len(name)
        if self.max_testname_len < name_len:
            self.max_testname_len = name_len


    def dump_table(self) -> None:
        console = Console()
        table = Table(show_header=True, header_style="bold")
        table.add_column("test", justify="left", no_wrap=True)
        table.add_column("config", justify="left", no_wrap=True)
        table.add_column("time", justify="right")
        table.add_column("passed/total", justify="right")
        table.add_column("failed", justify="right")
        table.add_column("skipped", justify="right")
        table.add_column("excluded", justify="right")
        self.stats.dump_table(table, self.report_all)
        print()
        console.print(table)


    def dump_junit(self, report_path: str) -> None:
        os.makedirs(report_path, exist_ok=True)

        self.stats.dump_junit_files(report_path)



    def get_config(self) -> str:
        return self.config

    def pop_test(self) -> TestRun | None:
        return self.queue.get()

    def start(self) -> None:
        if self.nb_threads == 0:
            self.nb_threads = psutil.cpu_count(logical=True) or 1

        self._interrupted: bool = False
        import threading as _threading
        if _threading.current_thread() is _threading.main_thread():
            self._orig_sigint: Any = signal.getsignal(
                signal.SIGINT
            )
            signal.signal(
                signal.SIGINT, self._handle_interrupt
            )
        else:
            self._orig_sigint = signal.SIG_DFL

        for thread_id in range(0, self.nb_threads):
            Worker(self).start()

    def _handle_interrupt(
        self, signum: int, frame: FrameType | None
    ) -> None:
        """Graceful Ctrl+C: stop everything."""
        if self._interrupted:
            # Second Ctrl+C: force exit
            signal.signal(signal.SIGINT, self._orig_sigint)
            raise KeyboardInterrupt
        self._interrupted = True
        print('\n--- Interrupted, killing running tests ---')
        sys.stdout.flush()

        self.lock.acquire()
        # Clear pending tests
        dropped: int = len(self.pending_tests)
        self.pending_tests.clear()
        self.nb_pending_tests -= dropped

        # Drain the queue so workers don't pick up more
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
                if item is not None:
                    self.nb_pending_tests -= 1
            except Exception:
                break

        # Kill all currently running test processes
        for run in list(self.active_runs):
            run.kill()

        if self.nb_pending_tests <= 0:
            self.nb_pending_tests = 0
            self.event.set()
        self.lock.release()

    def stop(self) -> None:
        for thread_id in range(0, self.nb_threads):
            self.queue.put(None)
        # Restore original signal handler
        import threading as _threading
        if (hasattr(self, '_orig_sigint')
                and self._orig_sigint is not None
                and _threading.current_thread()
                is _threading.main_thread()):
            signal.signal(signal.SIGINT, self._orig_sigint)
            self._orig_sigint = None

    def add_testset(self, file: str) -> None:
        if not os.path.isabs(file):
            file = os.path.join(os.getcwd(), file)

        # Resolve targets for the root testset's directory
        testset_dir = os.path.dirname(file)
        targets = self._resolve_targets_for_dir(testset_dir)

        if targets:
            for target in targets:
                self.testsets.append(
                    self.import_testset(file, target, None)
                )
        else:
            self.testsets.append(
                self.import_testset(
                    file, self.default_target, None
                )
            )

    def _has_own_targets(self, directory: str) -> bool:
        """Check if directory has its own gvtest.yaml with
        a targets section (not inherited from parent)."""
        config_file = os.path.join(
            directory, 'gvtest.yaml'
        )
        if not os.path.exists(config_file):
            return False
        try:
            loader = ConfigLoader(directory)
            config = loader.load_config(Path(config_file))
            return 'targets' in config
        except Exception:
            return False

    def _resolve_targets_for_dir(
        self, directory: str
    ) -> list[Target]:
        """Resolve targets for a specific directory from
        gvtest.yaml hierarchy. Returns list of Target objects
        applicable to this directory, filtered by CLI --target
        if specified."""
        loader = ConfigLoader(directory)
        loader.config_files = loader.discover_configs()
        yaml_targets = loader.resolve_targets(
            loader.config_files
        )

        if not yaml_targets:
            return []

        # Build Target objects
        targets: list[Target] = []
        for name, cfg in yaml_targets.items():
            # If CLI specifies targets, filter
            if (self.target_names != ['default']
                    and name not in self.target_names):
                continue
            targets.append(Target.from_dict(name, cfg))

        return targets


    def import_testset(
        self, file: str, target: Target,
        parent: TestsetImpl | None = None
    ) -> TestsetImpl:
        logging.debug(f"Parsing file (path: {file})")

        # Get the directory of the testset file
        testset_dir: str = os.path.dirname(file)
        
        # Discover and load gvtest.yaml configs for this testset's directory hierarchy
        # This will find all gvtest.yaml files from testset_dir up to filesystem root
        python_paths: list[str] = get_python_paths_for_dir(testset_dir)
        
        # Save the current sys.path to restore it later
        # This ensures complete isolation between testsets
        saved_sys_path: list[str] = sys.path.copy()
        
        try:
            # Add the discovered paths to sys.path
            # This allows the testset to import from configured paths during loading
            for path in python_paths:
                if path not in sys.path:
                    sys.path.insert(0, path)
                    logging.debug(f"Added to sys.path for testset: {path}")
            
            # Cache modules: import once, call testset_build per target.
            # This preserves global state across targets.
            module_name: str = f"gvtest_testset_{hash(file)}"
            if module_name in self._module_cache:
                module = self._module_cache[module_name]
            else:
                spec = importlib.util.spec_from_loader(module_name, SourceFileLoader(module_name, file))
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self._module_cache[module_name] = module

            # testset_build() must run while python_paths are still in sys.path,
            # since it may import modules from configured paths
            testset: TestsetImpl = TestsetImpl(self, target, parent, path=os.path.dirname(file))
            module.testset_build(testset)
        except FileNotFoundError as exc:
            raise RuntimeError('Unable to open test configuration file: ' + file)
        finally:
            # Restore original sys.path to maintain isolation between testsets
            # Imported modules remain available via sys.modules cache
            sys.path = saved_sys_path
            logging.debug(f"Restored sys.path after loading testset")

        return testset


    def count_test(self) -> None:
        """Increment total test count (incl. skipped)."""
        self.nb_total_tests += 1

    def enqueue_test(self, test: TestRun) -> None:
        self.lock.acquire()
        self.nb_pending_tests += 1
        self.pending_tests.append(test)
        self.lock.release()



    def check_pending_tests(self) -> None:
        while True:
            self.lock.acquire()
            if len(self.pending_tests) == 0:
                self.lock.release()
                break

            if self._interrupted:
                # Drop all remaining pending tests
                dropped: int = len(self.pending_tests)
                self.pending_tests.clear()
                self.nb_pending_tests -= dropped
                if self.nb_pending_tests <= 0:
                    self.nb_pending_tests = 0
                    self.event.set()
                self.lock.release()
                break

            # Find a test whose dependencies are met
            test: TestRun | None = None
            for i in range(
                len(self.pending_tests) - 1, -1, -1
            ):
                candidate = self.pending_tests[i]
                if candidate.test.deps_satisfied():
                    test = self.pending_tests.pop(i)
                    break

            if test is None:
                # All pending tests have unmet deps;
                # wait for running tests to finish
                self.lock.release()
                time.sleep(0.1)
                continue

            self.lock.release()

            while not self.check_cpu_load():
                time.sleep(self.cpu_poll_interval)

            self.queue.put(test)


    def check_cpu_load(self) -> bool:
        if self.load_average == 1.0:
            return True

        load: float = psutil.cpu_percent(interval=self.cpu_poll_interval)

        return load < self.load_average * 100


    def get_max_testname_len(self) -> int:
        return self.max_testname_len


    def register_active(self, test: TestRun) -> None:
        self.lock.acquire()
        self.active_runs.append(test)
        self.lock.release()

    def unregister_active(self, test: TestRun) -> None:
        self.lock.acquire()
        if test in self.active_runs:
            self.active_runs.remove(test)
        self.lock.release()

    def terminate(self, test: TestRun) -> None:
        self.unregister_active(test)
        self.lock.acquire()
        self.nb_pending_tests -= 1

        if self.nb_pending_tests == 0:
            self.event.set()

        self.lock.release()

    def register_bench_result(self, name: str, value: float, desc: str) -> None:
        self.bench_results[name] = [value, desc]
