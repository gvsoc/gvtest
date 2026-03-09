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

import os
import logging
import signal
import sys
import csv
import queue
import threading
import time
import importlib
from importlib.machinery import SourceFileLoader

import psutil
import rich.table
from prettytable import PrettyTable
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn
from rich.align import Align

from gvtest.config import get_python_paths_for_dir
from gvtest.targets import Target
from gvtest.stats import TestsetStats
from gvtest.reporting import bcolors
from gvtest.testset_impl import TestsetImpl

# Re-export classes that external code (tests, __main__) may import from runner
from gvtest.targets import Target
from gvtest.tests import (
    TestRun, TestCommon, TestImpl, MakeTestImpl,
    GvrunTestImpl, SdkTestImpl, NetlistPowerSdkTestImpl,
)
from gvtest.stats import TestRunStats, TestStats, TestsetStats
from gvtest.reporting import bcolors, table_dump_row


class Worker(threading.Thread):

    def __init__(self, runner):
        super().__init__(daemon=True)

        self.runner = runner

    def run(self):
        while True:
            test = self.runner.pop_test()
            if test is None:
                return
            test.run()


class Runner():

    def __init__(self, config='default', load_average=0.9, nb_threads=0, properties=None,
            stdout=False, safe_stdout=False, max_output_len=-1, max_timeout=-1,
            test_list=None, test_skip_list=None, commands=None, commands_exclude=None,
            flags=None, bench_csv_file=None, bench_regexp=None, targets=None, platform='gvsoc',
            report_all=False):
        self.nb_threads = nb_threads
        self.queue = queue.Queue()
        self.testsets = []
        self.pending_tests = []
        self.max_testname_len = 0
        self.config = config
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.load_average = load_average
        self.stdout = stdout
        self.safe_stdout = safe_stdout
        self.nb_pending_tests = 0
        self.test_skip_list = test_skip_list
        self.max_timeout = max_timeout
        self.max_output_len = max_output_len
        self.commands_filter = commands
        self.commands_exclude = commands_exclude
        self.flags = flags
        self.bench_results = {}
        self.bench_csv_file = bench_csv_file
        self.properties = {}
        self.test_list = test_list
        self.targets = targets
        self.platform = platform
        if self.targets is None:
            self.targets = [ 'default' ]
            self.default_target = Target('default')
        else:
            self.default_target = Target(self.targets[0])
        self.cpu_poll_interval = 0.1
        self.report_all = report_all
        for prop in properties:
          name, value = prop.split('=')
          self.properties[name] = value


        if bench_csv_file is not None:
            if os.path.exists(bench_csv_file):
                with open(bench_csv_file, 'r') as file:
                    csv_reader = csv.reader(file)
                    for row in csv_reader:
                        self.bench_results[row[0]] = row[1:]

    def get_active_targets(self):
        return self.targets

    def get_platform(self):
        return self.platform

    def get_property(self, name):
        return self.properties.get(name)

    def is_selected(self, test):
        if self.test_list is None:
            return True

        for selected_test in self.test_list:
            if test.get_full_name().find(selected_test) == 0:
                return True

        return False

    def is_skipped(self, name):
        if self.test_skip_list is not None:
            for skip in self.test_skip_list:
                if name.find(skip) == 0:
                    return True

        return False

    def tests(self):
        table = rich.table.Table(title=f'tests', title_justify="left")
        table.add_column('Name')
        table.add_column('Path')
        table.add_column('Targets')

        for testset in self.testsets:
            testset.dump_tests(table)

        print()
        rich.print(table)

    def summary(self):
        failed = self.stats.stats['failed']
        passed = self.stats.stats['passed']
        skipped = self.stats.stats['skipped']
        excluded = self.stats.stats['excluded']
        total = failed + passed

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

        success_ratio = passed / total if total > 0 else 0
        percent = int(success_ratio * 100)

        if passed == total:
            msg = "[bold green]All tests passed[/bold green]"
        else:
            msg = f"[bold red]{passed}/{total} tests passed ({percent}%).[/bold red]"

        final_bar = Progress(
            BarColumn(bar_width=len(msg))
        )

        task = final_bar.add_task("", total=100, completed=percent)

        if passed == total:
            content = msg
        else:
            content = Group(
                Align.center(msg, vertical="middle"),
                Align.center(final_bar, vertical="middle")
            )

        console.print(Panel.fit(content, border_style="green" if passed == total else "red", padding=(1,2)))

    def run(self):
        self.event.clear()

        for testset in self.testsets:
            testset.enqueue()

        if len(self.pending_tests) > 0:

            self.check_pending_tests()

            # Only wait if there are still tests running
            self.lock.acquire()
            should_wait = self.nb_pending_tests > 0
            self.lock.release()
            if should_wait:
                self.event.wait()

        self.stats = TestsetStats()
        for testset in self.testsets:
            self.stats.add_child_testset(testset)

        if self.bench_csv_file is not None:
            with open(self.bench_csv_file, 'w') as file:
                csv_writer = csv.writer(file)
                for key, value in self.bench_results.items():
                    csv_writer.writerow([key] + value)



    def declare_name(self, name):
        name_len = len(name)
        if self.max_testname_len < name_len:
            self.max_testname_len = name_len


    def dump_table(self):
        x = PrettyTable(['test', 'config', 'time', 'passed/total', 'failed', 'skipped', 'excluded'])
        x.align = "r"
        x.align["test"] = "l"
        x.align["config"] = "l"
        self.stats.dump_table(x, self.report_all)
        print()
        print(x)


    def dump_junit(self, report_path):
        os.makedirs(report_path, exist_ok=True)

        self.stats.dump_junit_files(report_path)



    def get_config(self):
        return self.config

    def pop_test(self):
        return self.queue.get()

    def start(self):
        if self.nb_threads == 0:
            self.nb_threads = psutil.cpu_count(logical=True)

        self._interrupted = False
        self._orig_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_interrupt)

        for thread_id in range(0, self.nb_threads):
            Worker(self).start()

    def _handle_interrupt(self, signum, frame):
        """Graceful Ctrl+C: drain pending tests and signal workers to stop."""
        if self._interrupted:
            # Second Ctrl+C: force exit
            signal.signal(signal.SIGINT, self._orig_sigint)
            raise KeyboardInterrupt
        self._interrupted = True
        print('\n--- Interrupted, stopping after current tests finish (Ctrl+C again to force) ---')
        sys.stdout.flush()
        # Clear pending tests so no new ones get queued
        self.lock.acquire()
        dropped = len(self.pending_tests)
        self.pending_tests.clear()
        self.nb_pending_tests -= dropped
        if self.nb_pending_tests <= 0:
            self.nb_pending_tests = 0
            self.event.set()
        self.lock.release()

    def stop(self):
        for thread_id in range(0, self.nb_threads):
            self.queue.put(None)
        # Restore original signal handler
        if hasattr(self, '_orig_sigint') and self._orig_sigint is not None:
            signal.signal(signal.SIGINT, self._orig_sigint)
            self._orig_sigint = None

    def add_testset(self, file):
        if not os.path.isabs(file):
            file = os.path.join(os.getcwd(), file)
        self.testsets.append(self.import_testset(file, self.default_target))


    def import_testset(self, file, target, parent=None):
        logging.debug(f"Parsing file (path: {file})")

        # Get the directory of the testset file
        testset_dir = os.path.dirname(file)
        
        # Discover and load gvtest.yaml configs for this testset's directory hierarchy
        # This will find all gvtest.yaml files from testset_dir up to filesystem root
        python_paths = get_python_paths_for_dir(testset_dir)
        
        # Save the current sys.path to restore it later
        # This ensures complete isolation between testsets
        saved_sys_path = sys.path.copy()
        
        try:
            # Add the discovered paths to sys.path
            # This allows the testset to import from configured paths during loading
            for path in python_paths:
                if path not in sys.path:
                    sys.path.insert(0, path)
                    logging.debug(f"Added to sys.path for testset: {path}")
            
            # Use a unique module name per file to avoid collisions in sys.modules
            module_name = f"gvtest_testset_{hash(file)}"
            spec = importlib.util.spec_from_loader(module_name, SourceFileLoader(module_name, file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # testset_build() must run while python_paths are still in sys.path,
            # since it may import modules from configured paths
            testset = TestsetImpl(self, target, parent, path=os.path.dirname(file))
            module.testset_build(testset)
        except FileNotFoundError as exc:
            raise RuntimeError(bcolors.FAIL + 'Unable to open test configuration file: ' + file + bcolors.ENDC)
        finally:
            # Restore original sys.path to maintain isolation between testsets
            # Imported modules remain available via sys.modules cache
            sys.path = saved_sys_path
            logging.debug(f"Restored sys.path after loading testset")

        return testset


    def enqueue_test(self, test):
        self.lock.acquire()
        self.nb_pending_tests += 1
        self.pending_tests.append(test)
        self.lock.release()



    def check_pending_tests(self):
        while True:
            self.lock.acquire()
            if len(self.pending_tests) == 0:
                self.lock.release()
                break

            if self._interrupted:
                # Drop all remaining pending tests
                dropped = len(self.pending_tests)
                self.pending_tests.clear()
                self.nb_pending_tests -= dropped
                if self.nb_pending_tests <= 0:
                    self.nb_pending_tests = 0
                    self.event.set()
                self.lock.release()
                break

            test = self.pending_tests.pop()
            self.lock.release()

            while not self.check_cpu_load():
                time.sleep(self.cpu_poll_interval)

            self.queue.put(test)


    def check_cpu_load(self):
        if self.load_average == 1.0:
            return True

        load = psutil.cpu_percent(interval=self.cpu_poll_interval)

        return load < self.load_average * 100


    def get_max_testname_len(self):
        return self.max_testname_len


    def terminate(self, test):
        self.lock.acquire()
        self.nb_pending_tests -= 1

        if self.nb_pending_tests == 0:
            self.event.set()

        self.lock.release()

    def register_bench_result(self, name, value, desc):
        self.bench_results[name] = [value, desc]
