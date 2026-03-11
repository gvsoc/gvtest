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
Test execution — TestRun, TestCommon, and all test implementation classes.
"""

from __future__ import annotations

import traceback
import os
import io
import re
import sys
import signal
import subprocess
import threading
from datetime import datetime
from threading import Timer
from typing import Any, Callable

import psutil

import gvtest.testsuite as testsuite
from rich.console import Console
from rich.table import Table

_console = Console(highlight=False)


class TestRun(object):

    def __init__(self, test: TestCommon, target: Any | None) -> None:
        self.target: Any | None = target
        self.test: TestCommon = test
        self.runner: Any = test.runner
        self.lock: threading.Lock = threading.Lock()
        self.duration: float = 0
        if target is not None:
            self.config: str = target.name
        else:
            self.config = self.runner.config

        self.sourceme: str | None = None
        self.envvars: dict[str, str] | None = None
        self.skip_message: str = ""
        self.status: str = "failed"
        self.output: str = ""
        self.timeout_reached: bool = False
        self.current_proc: subprocess.Popen[bytes] | None = None

        if self.target is not None:
            self.sourceme = self.target.get_sourceme()
            self.envvars = self.target.get_envvars()

    def get_target_name(self) -> str:
        if self.target is None:
            return self.config

        return self.target.name

    def get_stats(self, stats: dict[str, int | float]) -> None:
        stats[self.status] += 1
        stats['duration'] = self.duration

    # Called by worker to execute the test
    def run(self) -> None:
        # Check if runner was interrupted before we start
        if self.runner._interrupted:
            self.output = ''
            self.status = "failed"
            self.duration = 0
            self.runner.terminate(self)
            return

        self.runner.register_active(self)
        self.__print_start_message()

        self.output: str = ''
        self._output_truncated: bool = False
        self.status: str = "passed"

        start_time: datetime = datetime.now()

        timeout: int = self.runner.max_timeout
        self.timeout_reached: bool = False
        timer: Timer | None = None

        if timeout != -1:
            timer = Timer(timeout, self.kill)
            timer.start()

        for command in self.test.commands:

            # Apply --cmd / --cmd-exclude filters
            cmd_name: str | None = getattr(command, 'name', None)
            if cmd_name is not None:
                if self.runner.commands_filter is not None:
                    if cmd_name not in self.runner.commands_filter:
                        continue
                if self.runner.commands_exclude is not None:
                    if cmd_name in self.runner.commands_exclude:
                        continue

            retval: int = self.__exec_command(
                command, self.target,
                self.sourceme, self.envvars
            )

            if self.runner._interrupted:
                self.__dump_test_msg(
                    '--- Interrupted ---\n'
                )
                self.status = "failed"
                break

            if retval != 0 or self.timeout_reached:
                if self.timeout_reached:
                    self.__dump_test_msg(
                        '--- Timeout reached ---\n'
                    )
                self.status = "failed"
                break

        if timeout != -1 and timer is not None:
            timer.cancel()

        duration = datetime.now() - start_time
        self.duration = \
            (duration.microseconds +
                (duration.seconds + duration.days * 24 * 3600) * 10**6) / 10**6

        if self.runner.safe_stdout:
            print (self.output)

        for bench in self.test.benchs:
            pattern = re.compile(bench[0])
            for line in self.output.splitlines():
                result = pattern.match(line)
                if result is not None:
                    value = float(result.group(1))
                    name = bench[1]
                    desc = bench[2]
                    self.runner.register_bench_result(name, value, desc)


        self.print_end_message()

        self.runner.terminate(self)

    def kill(self) -> None:
        self.lock.acquire()
        self.timeout_reached = True
        if self.current_proc is not None:
            try:
                # Kill the entire process group (created by
                # start_new_session=True) — this immediately
                # terminates the test and all its children
                pgid = os.getpgid(self.current_proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already exited
                pass
            except OSError:
                # Fallback: kill individual processes
                try:
                    process = psutil.Process(
                        pid=self.current_proc.pid
                    )
                    for child in process.children(
                        recursive=True
                    ):
                        os.kill(child.pid, signal.SIGKILL)
                    self.current_proc.kill()
                except Exception:
                    pass
        self.lock.release()

    # Print start banner
    def print_start(self) -> None:
        """Public wrapper for start message."""
        self.__print_start_message()

    def __print_start_message(self) -> None:
        testname: str = (
            self.test.get_full_name() or ''
        ).ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config: str = self.target.name
        else:
            config = self.runner.get_config()
        if self.runner.tui is not None:
            self.runner.tui.test_started(
                id(self), testname.strip(), config
            )
        elif self.runner.live_display is not None:
            self.runner.live_display.test_started(
                id(self), testname.strip(), config
            )
        else:
            _console.print(
                f"[blue]{'START'.ljust(8)}[/blue]"
                f"[bold]{testname}[/bold] {config}"
            )

    # Print end banner
    def print_end_message(self) -> None:
        testname: str = (
            self.test.get_full_name() or ''
        ).ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config: str = self.target.name
        else:
            config = self.runner.get_config()

        status_styles: dict[str, tuple[str, str]] = {
            'passed':   ('[green]', 'OK'),
            'failed':   ('[red]',   'KO'),
            'skipped':  ('[yellow]', 'SKIP'),
            'excluded': ('[magenta]', 'EXCLUDE'),
        }
        style, label = status_styles.get(
            self.status, ('[white]', '???')
        )
        msg = (
            f"{style}{label.ljust(8)}[/] "
            f"[bold]{testname}[/bold] {config}"
        )

        if self.runner.tui is not None:
            self.runner.tui.test_finished(
                id(self), self.status,
                testname.strip(), config
            )
        elif self.runner.live_display is not None:
            self.runner.live_display.test_finished(
                id(self), self.status
            )
            self.runner.live_display.log(msg)
        else:
            _console.print(msg)

    def __exec_process(self, command: str, envvars: dict[str, str] | None = None) -> int:
        self.lock.acquire()
        if self.timeout_reached:
            return -1

        env: dict[str, str] = os.environ.copy()

        if envvars is not None:
            env.update(envvars)

        # Use a PTY to isolate terminal state — prevents
        # tests from corrupting the parent terminal
        # (e.g. raw mode, no-echo from SDL2/ncurses)
        master_fd, slave_fd = os.openpty()

        proc: subprocess.Popen[bytes] = subprocess.Popen(
            command, stdout=slave_fd,
            stderr=slave_fd, stdin=slave_fd,
            shell=True, cwd=self.test.path, env=env,
            start_new_session=True
        )

        # Close slave in parent — only the child uses it
        os.close(slave_fd)

        self.current_proc = proc

        self.lock.release()

        # Read from master fd
        master_file = os.fdopen(master_fd, 'rb')
        try:
            for line in io.TextIOWrapper(
                master_file, encoding="utf-8",
                errors='replace'
            ):
                self.__dump_test_msg(line)
        except IOError:
            # PTY closed when process exits
            pass

        retval: int = proc.wait()
        self.current_proc = None

        return retval


    def __dump_test_msg(self, msg: str) -> None:
        max_len: int = self.runner.max_output_len
        if max_len != -1 and self._output_truncated:
            return  # Already truncated, discard further output
        self.output += msg
        if max_len != -1 and len(self.output) > max_len:
            self.output = self.output[:max_len]
            self.output += '\n--- Output truncated at %d bytes ---\n' % max_len
            self._output_truncated = True
        if self.runner.stdout:
            print (msg[:-1])


    # Called by run method to execute specific command
    def __exec_command(
        self, command: testsuite.Command,
        target: Any | None, sourceme: str | None,
        envvars: dict[str, str] | None
    ) -> int:

        if type(command) == testsuite.Shell:
            cmd: str = command.cmd
            if self.target is not None:
                cmd = self.target.format_properties(cmd)

            self.__dump_test_msg(f'--- Shell command: {cmd} ---\n')

            if sourceme is not None:
                cmd = f'gvtest_cmd_stub {sourceme} {cmd}'

            retval: int = 0 if self.__exec_process(cmd, envvars) == command.retval else 1

        elif type(command) == testsuite.Checker:
            self.__dump_test_msg(f'--- Checker command ---\n')
            try:
                result = command.callback[0](
                    self, self.output,
                    *command.callback[1],
                    **command.callback[2]
                )
            except:
                result = (False, "Got exception: " + traceback.format_exc())

            if result[1] is not None:
                self.__dump_test_msg(result[1])
            retval = 0 if result[0] else -1

        elif type(command) == testsuite.Call:
            self.__dump_test_msg(f'--- Call command ---\n')
            return command.callback()

        else:
            print ('Unsupported command type: ' + str(type(command)))
            retval = -1

        return retval


class TestCommon(object):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any | None,
        path: str | None
    ) -> None:
        self.runner: Any = runner
        self.target: Any | None = target
        self.name: str = name
        self.parent: Any | None = parent
        self.full_name: str | None = None
        self.commands: list[testsuite.Command] = []
        self.path: str | None = path
        self.status: str | None = None
        self.skipped: str | None = None
        self.description: str | None = None
        if self.path == '':
            self.path = os.getcwd()
        self.current_proc: subprocess.Popen[bytes] | None = None

        self.full_name = self.name

        if self.parent is not None:
            parent_name: str | None = self.parent.get_full_name()
            if parent_name is not None:
                self.full_name =  f'{parent_name}:{self.name}'

        self.runner.declare_name(self.full_name)
        self.benchs: list[list[str]] = []
        self.runs: list[TestRun] = []

    def skip(self, msg: str) -> TestCommon:
        self.skipped = msg
        return self

    def get_target(self) -> Any | None:
        return self.target

    # Called by user to add commands
    def add_command(self, command: testsuite.Command) -> None:
        self.commands.append(command)


    # Called by runner to enqueue this test to the list of tests ready to be executed
    def enqueue(self) -> None:
        run: TestRun = TestRun(self, self.target)
        config = (
            self.target.name if self.target is not None
            else self.runner.get_config()
        )
        self.runner.count_test()
        if self.runner.tui is not None:
            self.runner.tui.count_target(config)
        if self.runner.is_skipped(self.get_full_name()) or self.skipped is not None:
            if self.skipped is not None:
                run.skip_message = self.skipped
            else:
                run.skip_message = "Skipped from command line"
            run.status = "skipped"
            self.runs.append(run)
            run.print_end_message()

        else:
            self.runs.append(run)
            self.runner.enqueue_test(run)

    def dump_tests(self, table: Table, indent: str, targets: list[str]) -> None:
        table.add_row(indent + self.name, self.get_full_name(), ", ".join(targets))

    # Can be called to get full name including hierarchy path
    def get_full_name(self) -> str | None:
        return self.full_name

    def get_path(self) -> str:
        assert self.full_name is not None
        return self.full_name.replace(':', '/')

    def add_description(self, description: str) -> None:
        self.description = description


class TestImpl(TestCommon, testsuite.Test):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any | None,
        path: str | None
    ) -> None:
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name

    def add_bench(self, extract: str, name: str, desc: str) -> None:
        self.benchs.append([extract, name, desc])


class MakeTestImpl(TestCommon, testsuite.Test):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any | None,
        path: str | None, flags: str | None,
        checker: Callable[..., Any] | None = None,
        retval: int = 0
    ) -> None:
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags: str | None = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform: str | None = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' platform=%s' % platform

        workdir: str | None = os.environ.get('GVSOC_WORKDIR')
        if workdir is None:
            builddir: str = f'{path}/build/{runner.get_config()}/{self.name}'
        else:
            builddir = f'{workdir}/tests/{self.get_path()}'
        self.flags += f' build={builddir}'

        self.add_command(testsuite.Shell('clean', 'make clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'make build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'make run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract: str, name: str, desc: str) -> None:
        self.benchs.append([extract, name, desc])


class GvrunTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any,
        path: str | None, flags: str | None,
        checker: Callable[..., Any] | None = None,
        retval: int = 0
    ) -> None:
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags: str | None = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform: str | None = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        target = target.get_name()

        workdir: str | None = os.environ.get('GVSOC_WORKDIR')
        if workdir is None:
            builddir: str = f'build/{target}/{self.name}'
        else:
            builddir = f'{workdir}/tests/{self.get_path()}/{target}'
        self.flags += f' --work-dir={builddir}'

        cmd: str = f'gvrun --target {target} {self.flags}'
        self.add_command(testsuite.Shell('clean', f'{cmd} clean'))
        self.add_command(testsuite.Shell('build', f'{cmd} build'))
        self.add_command(testsuite.Shell('run', f'{cmd} run', retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract: str, name: str, desc: str) -> None:
        self.benchs.append([extract, name, desc])


class SdkTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any,
        path: str | None, flags: str | None,
        checker: Callable[..., Any] | None = None,
        retval: int = 0
    ) -> None:
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags: str | None = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform: str | None = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        self.flags += f' --build=build/{runner.get_config()}/{self.name}'

        self.add_command(testsuite.Shell('clean', 'posbuild clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'posbuild build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'posbuild run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract: str, name: str, desc: str) -> None:
        self.benchs.append([extract, name, desc])


class NetlistPowerSdkTestImpl(SdkTestImpl):

    def __init__(
        self, runner: Any, parent: Any | None,
        name: str, target: Any,
        path: str | None, flags: str | None
    ) -> None:
        SdkTestImpl.__init__(self, runner, parent, name, target, path, flags)

        self.add_command(testsuite.Shell('power_gen', 'make power_gen %s' % (self.flags)))
        self.add_command(testsuite.Shell('power_copy', 'make power_copy %s' % (self.flags)))
