#!/usr/bin/env python3

#
# Copyright (C) 2023 ETH Zurich, University of Bologna
# and GreenWaves Technologies
#
# Licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of
# the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the License is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing
# permissions and limitations under the License.
#

"""
Run events — how the front ends follow a test run.

The runner and the test runs report what happens to every listener
registered with `Runner.add_listener()`. The console output, the progress
bar, the curses TUI and the web GUI are all listeners; none of them is
known to the runner.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console


STATUS_STYLES: dict[str, tuple[str, str]] = {
    'passed':   ('[green]', 'OK'),
    'failed':   ('[red]',   'KO'),
    'skipped':  ('[yellow]', 'SKIP'),
    'excluded': ('[magenta]', 'EXCLUDE'),
}


def start_message(run: Any, pad: bool = True) -> str:
    """The START line of a run, in rich markup."""
    name: str = run.get_display_name()
    if not pad:
        name = name.strip()
    return (
        f"[blue]{'START'.ljust(8)}[/blue]"
        f"[bold]{name}[/bold] {run.config}"
    )


def end_message(run: Any) -> str:
    """The OK/KO/SKIP line of a run, in rich markup."""
    style, label = STATUS_STYLES.get(run.status, ('[white]', '???'))
    name: str = run.get_display_name()
    return f"{style}{label.ljust(8)}[/] [bold]{name}[/bold] {run.config}"


class RunListener:
    """Base class of the run listeners; every event defaults to a no-op.

    Events are called from the runner thread, the worker threads and the
    pytest batch threads, so a listener must do its own locking. `run` is
    always the `TestRun` the event is about.
    """

    # True for a listener that draws the whole terminal itself; the runner
    # then adds neither the console output nor the progress bar.
    owns_terminal: bool = False

    def test_counted(self, run: Any) -> None:
        """A run was created, whether it will execute or is skipped."""

    def set_total(self, total: int) -> None:
        """All the runs of this pass were counted."""

    def test_started(self, run: Any) -> None:
        """The run starts executing."""

    def test_output(self, run: Any) -> None:
        """`run.output` grew."""

    def test_finished(self, run: Any) -> None:
        """The run got its status. A pytest run reports it as soon as the
        batch prints it, before its output and duration are known."""

    def test_updated(self, run: Any) -> None:
        """The output or the duration of a finished run was filled in."""

    def run_finished(self) -> None:
        """The pass is over: nothing is running or pending any more."""


class ConsoleListener(RunListener):
    """Prints one START and one OK/KO line per run."""

    def __init__(self, console: Console | None = None) -> None:
        self.console: Console = console if console is not None else \
            Console(highlight=False, stderr=True)

    def test_started(self, run: Any) -> None:
        self.console.print(start_message(run))

    def test_finished(self, run: Any) -> None:
        self.console.print(end_message(run))
