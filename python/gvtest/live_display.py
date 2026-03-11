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
Live progress display using Rich.

Shows OK/KO results scrolling normally in the terminal
with a sticky progress bar at the bottom.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


def _format_duration(seconds: float) -> str:
    """Format duration as human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:02.0f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


class LiveDisplay:
    """Progress bar that sticks to the bottom.

    OK/KO results scroll normally above; progress bar
    with pass/fail/skip counters updates in place.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.lock = threading.Lock()
        self.total: int = 0
        self.completed: int = 0
        self.passed: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.live: Live | None = None
        self.start_time: datetime = datetime.now()
        self._started = False

    def set_total(self, total: int) -> None:
        """Update the total test count."""
        with self.lock:
            self.total = total
            self._update()

    def start(self, total: int) -> None:
        """Start the live display."""
        self.total = total
        self.start_time = datetime.now()
        self._started = True
        self.live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self.live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self.live is not None:
            self.live.stop()
            self.live = None
        self._started = False

    def test_started(
        self, test_id: int, name: str, config: str
    ) -> None:
        """No-op for progress mode (no running panel)."""
        pass

    def test_finished(
        self, test_id: int, status: str
    ) -> None:
        if not self._started:
            return
        with self.lock:
            self.completed += 1
            if status == 'passed':
                self.passed += 1
            elif status == 'failed':
                self.failed += 1
            elif status in ('skipped', 'excluded'):
                self.skipped += 1
            self._update()

    def log(self, message: str) -> None:
        """Print a message above the live display."""
        if self.live is not None:
            self.live.console.print(
                message, highlight=False
            )

    def _update(self) -> None:
        if self.live is not None:
            self.live.update(self._render())

    def _render(self) -> Text:
        """Render the progress bar with colored segments."""
        elapsed = _format_duration(
            (datetime.now() -
             self.start_time).total_seconds()
        )

        bar_width = 30
        if self.total > 0:
            # Minimum 1 char for any non-zero count
            green_w = (
                max(1, round(
                    self.passed / self.total * bar_width
                )) if self.passed > 0 else 0
            )
            red_w = (
                max(1, round(
                    self.failed / self.total * bar_width
                )) if self.failed > 0 else 0
            )
            skip_w = (
                max(1, round(
                    self.skipped / self.total * bar_width
                )) if self.skipped > 0 else 0
            )
            remaining = self.total - self.completed
            remain_w = (
                max(1, round(
                    remaining / self.total * bar_width
                )) if remaining > 0 else 0
            )
            # Fix rounding to exactly bar_width
            used = green_w + red_w + skip_w + remain_w
            diff = used - bar_width
            # Shrink the largest segment to compensate
            if diff != 0:
                segments = [
                    ('green', green_w),
                    ('remain', remain_w),
                    ('skip', skip_w),
                    ('red', red_w),
                ]
                segments.sort(key=lambda x: -x[1])
                for name, val in segments:
                    if val + (-diff) >= 1:
                        if name == 'green':
                            green_w -= diff
                        elif name == 'remain':
                            remain_w -= diff
                        elif name == 'skip':
                            skip_w -= diff
                        elif name == 'red':
                            red_w -= diff
                        break
        else:
            green_w = red_w = skip_w = 0
            remain_w = bar_width

        bar = Text()
        bar.append("━" * green_w, style="green")
        bar.append("━" * red_w, style="red")
        bar.append("━" * skip_w, style="yellow")
        bar.append("─" * remain_w, style="dim")

        result = Text()
        result.append("Tests ")
        result.append_text(bar)
        result.append(
            f" {self.completed}/{self.total} │ "
        )
        result.append(f"✓ {self.passed}", style="green")
        result.append("  ")
        result.append(f"✗ {self.failed}", style="red")
        result.append("  ")
        result.append(f"⊘ {self.skipped}", style="yellow")
        result.append(f" │ {elapsed}", style="dim")

        return result
