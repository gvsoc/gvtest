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
Curses-based TUI for gvtest.

Split-pane layout:
┌─ Results ─────────────────┬─ Running ─────────────┐
│ OK  test_3     default    │ ⏳ test_0   1.2s      │
│ KO  test_5     siracusa   │ ⏳ test_1   0.8s      │
├─ Progress ────────────────┴───────────────────────┤
│ ████░░░░  45/120  ✓42 ✗2 ⊘1              3m12s   │
└───────────────────────────────────────────────────┘
"""

from __future__ import annotations

import curses
import threading
import time
from datetime import datetime
from typing import Any


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:02.0f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


class CursesTUI:
    """Curses-based split-pane TUI."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self.lock = threading.Lock()

        # Results: (label, name, config, color, elapsed_s)
        self.results: list[
            tuple[str, str, str, int, float]
        ] = []
        self.results_scroll: int = -1  # -1 = follow tail

        # Running tests: {id: (name, config, start_time)}
        self.running: dict[int, tuple[str, str, datetime]] = {}

        # Stats (global)
        self.total: int = 0
        self.completed: int = 0
        self.passed: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.start_time: datetime = datetime.now()

        # Per-target stats: {config: {total, passed, ...}}
        self.target_stats: dict[str, dict[str, Any]] = {}

        self._done = threading.Event()
        self._end_time: datetime | None = None
        self._redraw = threading.Event()
        self._quit = False

    def count_target(self, config: str) -> None:
        """Increment total count for a target."""
        with self.lock:
            if config not in self.target_stats:
                self.target_stats[config] = {
                    'total': 0, 'passed': 0,
                    'failed': 0, 'skipped': 0,
                    'completed': 0,
                    'start_time': datetime.now(),
                    'end_time': None,
                }
            self.target_stats[config]['total'] += 1

    def test_started(
        self, test_id: int, name: str, config: str
    ) -> None:
        with self.lock:
            self.running[test_id] = (
                name, config, datetime.now()
            )
            self._redraw.set()

    def test_finished(
        self, test_id: int, status: str,
        name: str, config: str
    ) -> None:
        with self.lock:
            # Get elapsed from running entry
            run_info = self.running.pop(test_id, None)
            if run_info is not None:
                elapsed = (
                    datetime.now() - run_info[2]
                ).total_seconds()
            else:
                elapsed = 0.0

            self.completed += 1
            if status == 'passed':
                self.passed += 1
                color = 3  # green
                label = 'OK'
            elif status == 'failed':
                self.failed += 1
                color = 2  # red
                label = 'KO'
            elif status == 'skipped':
                self.skipped += 1
                color = 4  # yellow
                label = 'SKIP'
            elif status == 'excluded':
                self.skipped += 1
                color = 5  # magenta
                label = 'EXCLUDE'
            else:
                color = 0
                label = '???'
            self.results.append(
                (label, name, config, color, elapsed)
            )
            # Per-target stats
            if config not in self.target_stats:
                self.target_stats[config] = {
                    'total': 0, 'passed': 0,
                    'failed': 0, 'skipped': 0,
                    'completed': 0,
                }
            ts = self.target_stats[config]
            ts['completed'] += 1
            ts['end_time'] = datetime.now()
            if status == 'passed':
                ts['passed'] += 1
            elif status == 'failed':
                ts['failed'] += 1
            elif status in ('skipped', 'excluded'):
                ts['skipped'] += 1
            self._redraw.set()

    def set_total(self, total: int) -> None:
        with self.lock:
            self.total = total
            self._redraw.set()

    def _run_tests(self) -> None:
        """Run tests in background thread."""
        import sys
        try:
            self.runner.start()
            self.runner.run()
            self.runner.stop()
        except Exception as e:
            with self.lock:
                self.results.append(
                    ("ERROR", str(e), "", 2, 0.0)
                )
        finally:
            self._end_time = datetime.now()
            self._done.set()
            self._redraw.set()

    def run(self, stdscr: Any) -> None:
        """Main curses loop."""
        curses.curs_set(0)
        curses.set_escdelay(25)  # 25ms ESC delay
        stdscr.nodelay(True)
        stdscr.timeout(250)  # 4fps refresh

        # Colors
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.init_pair(6, curses.COLOR_BLUE, -1)
        # Progress bar colors (background)
        curses.init_pair(7, curses.COLOR_BLACK,
                         curses.COLOR_GREEN)
        curses.init_pair(8, curses.COLOR_BLACK,
                         curses.COLOR_RED)
        curses.init_pair(9, curses.COLOR_BLACK,
                         curses.COLOR_YELLOW)
        curses.init_pair(10, curses.COLOR_WHITE,
                         curses.COLOR_BLACK)

        # Start runner thread
        t = threading.Thread(
            target=self._run_tests, daemon=True
        )
        t.start()

        while not self._quit:
            self._draw(stdscr)

            key = stdscr.getch()
            if key == ord('q') or key == 27:  # q or ESC
                self._quit = True
            elif key == curses.KEY_UP:
                self._scroll_up()
            elif key == curses.KEY_DOWN:
                self._scroll_down(stdscr)
            elif key == curses.KEY_PPAGE:  # PageUp
                self._page_up(stdscr)
            elif key == curses.KEY_NPAGE:  # PageDown
                self._page_down(stdscr)
            elif key == curses.KEY_HOME:
                with self.lock:
                    self.results_scroll = 0
            elif key == curses.KEY_END:
                with self.lock:
                    self.results_scroll = -1
            elif key == curses.KEY_RESIZE:
                stdscr.clear()

            # When done, keep displaying until user quits
            if self._done.is_set():
                self._draw(stdscr)
                stdscr.timeout(-1)  # blocking wait
                k = stdscr.getch()
                if k == ord('q') or k == 27 or k == 10:
                    # q, ESC, or Enter to quit
                    break
                elif k == curses.KEY_UP:
                    self._scroll_up()
                elif k == curses.KEY_DOWN:
                    self._scroll_down(stdscr)
                elif k == curses.KEY_PPAGE:
                    self._page_up(stdscr)
                elif k == curses.KEY_NPAGE:
                    self._page_down(stdscr)
                elif k == curses.KEY_HOME:
                    with self.lock:
                        self.results_scroll = 0
                elif k == curses.KEY_END:
                    with self.lock:
                        self.results_scroll = -1
                continue

    def _scroll_up(self) -> None:
        with self.lock:
            if self.results_scroll == -1:
                # Switch from tail to explicit position
                self.results_scroll = max(
                    0, len(self.results) - 2
                )
            else:
                self.results_scroll = max(
                    0, self.results_scroll - 1
                )

    def _scroll_down(self, stdscr: Any) -> None:
        with self.lock:
            if self.results_scroll == -1:
                return
            self.results_scroll += 1
            if self.results_scroll >= len(self.results):
                self.results_scroll = -1

    def _page_up(self, stdscr: Any) -> None:
        h, _ = stdscr.getmaxyx()
        page = max(1, h - 6)
        with self.lock:
            if self.results_scroll == -1:
                self.results_scroll = max(
                    0, len(self.results) - page
                )
            else:
                self.results_scroll = max(
                    0, self.results_scroll - page
                )

    def _page_down(self, stdscr: Any) -> None:
        h, _ = stdscr.getmaxyx()
        page = max(1, h - 6)
        with self.lock:
            if self.results_scroll == -1:
                return
            self.results_scroll += page
            if self.results_scroll >= len(self.results):
                self.results_scroll = -1

    def _draw(self, stdscr: Any) -> None:
        """Draw the full screen."""
        h, w = stdscr.getmaxyx()
        if h < 5 or w < 20:
            return

        stdscr.erase()

        # Layout: progress bar = bottom 3 lines
        # Top area split: left 2/3, right 1/3
        progress_h = 3
        top_h = h - progress_h
        left_w = w // 2
        right_w = w - left_w

        # Right side: running on top, targets on bottom.
        # Show all targets, keep at least 3 rows for running
        # (1 content + 2 border).
        n_targets = max(1, len(self.target_stats))
        targets_h = min(n_targets + 2, top_h - 3)
        running_h = top_h - targets_h

        with self.lock:
            self._draw_results(
                stdscr, 0, 0, top_h, left_w
            )
            self._draw_running(
                stdscr, 0, left_w, running_h, right_w
            )
            self._draw_targets(
                stdscr, running_h, left_w,
                targets_h, right_w
            )
            self._draw_progress(
                stdscr, top_h, 0, progress_h, w
            )

        stdscr.noutrefresh()
        curses.doupdate()

    def _draw_box(
        self, stdscr: Any, y: int, x: int,
        h: int, w: int, title: str,
        color: int = 6
    ) -> None:
        """Draw a bordered box with title."""
        cp = curses.color_pair(color)
        # Top border
        self._safe_addstr(
            stdscr, y, x,
            "┌" + "─" * (w - 2) + "┐", cp
        )
        # Title
        if title:
            t = f" {title} "
            self._safe_addstr(
                stdscr, y, x + 2, t,
                cp | curses.A_BOLD
            )
        # Sides
        for row in range(y + 1, y + h - 1):
            self._safe_addstr(stdscr, row, x, "│", cp)
            self._safe_addstr(
                stdscr, row, x + w - 1, "│", cp
            )
        # Bottom
        if y + h - 1 < curses.LINES:
            self._safe_addstr(
                stdscr, y + h - 1, x,
                "└" + "─" * (w - 2) + "┘", cp
            )

    def _safe_addstr(
        self, stdscr: Any, y: int, x: int,
        text: str, attr: int = 0
    ) -> None:
        """Write string, truncating to fit."""
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        max_len = w - x - 1
        if max_len <= 0:
            return
        try:
            stdscr.addnstr(y, x, text, max_len, attr)
        except curses.error:
            pass

    def _draw_results(
        self, stdscr: Any, y: int, x: int,
        h: int, w: int
    ) -> None:
        """Draw the results panel."""
        title = "Results"
        if self.results_scroll != -1:
            title += " (paused — End to resume)"
        self._draw_box(stdscr, y, x, h, w, title)

        inner_h = h - 2
        inner_w = w - 4
        inner_y = y + 1
        inner_x = x + 2

        n = len(self.results)
        if self.results_scroll == -1:
            # Tail mode: show last inner_h lines
            start = max(0, n - inner_h)
        else:
            start = self.results_scroll

        # Layout: LABEL  testname ... target  time
        label_w = 9
        time_w = 7   # " XX.Xs" or " Xm00s"
        config_w = 15
        right_w = config_w + time_w
        name_w = max(10, inner_w - label_w - right_w)

        for i in range(inner_h):
            idx = start + i
            if idx >= n:
                break
            label, name, config, color, elapsed = (
                self.results[idx]
            )
            cx = inner_x
            # Status label (colored, bold)
            self._safe_addstr(
                stdscr, inner_y + i, cx,
                f"{label:<8}",
                curses.color_pair(color) | curses.A_BOLD
            )
            cx += label_w
            # Test name (white, normal)
            self._safe_addstr(
                stdscr, inner_y + i, cx,
                name[:name_w],
                curses.color_pair(1)
            )
            # Right-aligned: target + time
            right_edge = inner_x + inner_w
            t_str = _fmt_dur(elapsed)
            # Time (far right, dim)
            self._safe_addstr(
                stdscr, inner_y + i,
                right_edge - len(t_str),
                t_str,
                curses.color_pair(1) | curses.A_DIM
            )
            # Config (before time, dim)
            cfg_x = right_edge - len(t_str) - 1 - len(
                config[:config_w]
            )
            self._safe_addstr(
                stdscr, inner_y + i, cfg_x,
                config[:config_w],
                curses.color_pair(1) | curses.A_DIM
            )

    def _draw_running(
        self, stdscr: Any, y: int, x: int,
        h: int, w: int
    ) -> None:
        """Draw the running tests panel."""
        title = f"Running ({len(self.running)})"
        self._draw_box(
            stdscr, y, x, h, w, title, color=6
        )

        inner_h = h - 2
        inner_w = w - 4
        inner_y = y + 1
        inner_x = x + 2

        if not self.running:
            self._safe_addstr(
                stdscr, inner_y, inner_x,
                "No tests running",
                curses.color_pair(1) | curses.A_DIM
            )
            return

        now = datetime.now()
        sorted_tests = sorted(
            self.running.values(),
            key=lambda t: t[2]
        )
        time_w = 7
        config_w = min(15, inner_w // 4)
        right_w = config_w + time_w
        name_w = max(5, inner_w - right_w - 2)

        for i, (name, config, start) in enumerate(
            sorted_tests
        ):
            if i >= inner_h:
                break
            dur = (now - start).total_seconds()
            # Test name (white)
            self._safe_addstr(
                stdscr, inner_y + i, inner_x,
                f"  {name[:name_w]}",
                curses.color_pair(1)
            )
            # Right-aligned: config + time
            right_edge = inner_x + inner_w
            t_str = _fmt_dur(dur)
            self._safe_addstr(
                stdscr, inner_y + i,
                right_edge - len(t_str),
                t_str,
                curses.color_pair(1) | curses.A_DIM
            )
            cfg = config[:config_w]
            cfg_x = right_edge - len(t_str) - 1 - len(cfg)
            self._safe_addstr(
                stdscr, inner_y + i, cfg_x,
                cfg,
                curses.color_pair(1) | curses.A_DIM
            )

    def _draw_targets(
        self, stdscr: Any, y: int, x: int,
        h: int, w: int
    ) -> None:
        """Draw per-target progress bars."""
        title = "Targets"
        self._draw_box(
            stdscr, y, x, h, w, title, color=6
        )

        inner_h = h - 2
        inner_w = w - 4
        inner_y = y + 1
        inner_x = x + 2

        if not self.target_stats:
            self._safe_addstr(
                stdscr, inner_y, inner_x,
                "No targets yet",
                curses.color_pair(1) | curses.A_DIM
            )
            return

        # Compute column widths from data
        stats_list = sorted(self.target_stats.items())

        max_name_w = max(
            (len(t) for t, _ in stats_list), default=5
        )
        # Reserve width for max possible values (=total)
        max_count_w = max(
            (len(str(s['total'])) for _, s in stats_list),
            default=1
        )
        max_pass_w = max_count_w
        max_fail_w = max_count_w
        max_skip_w = max_count_w

        # Layout: name | bar | ✓P ✗F ⊘S D/T time
        time_w = 6  # "Xm00s" or "XX.Xs"
        count_col_w = max_count_w * 2 + 1
        stats_w = (
            1 + max_count_w + 1   # ✓P
            + 1 + max_count_w + 1  # ✗F
            + 1 + max_count_w + 1  # ⊘S
            + count_col_w + 1      # D/T
            + time_w              # time
        )
        name_w = min(max_name_w, inner_w // 3)
        bar_w = max(
            3, inner_w - name_w - 1 - stats_w - 1
        )

        for i, (target, st) in enumerate(stats_list):
            if i >= inner_h:
                break
            total = st['total']
            passed = st['passed']
            failed = st['failed']
            skipped = st['skipped']

            # Target name (left)
            cx = inner_x
            self._safe_addstr(
                stdscr, inner_y + i, cx,
                f"{target:<{name_w}}",
                curses.color_pair(1)
            )
            cx += name_w + 1

            # Progress bar (middle)
            completed = st['completed']
            if total > 0 and completed > 0:
                # Total filled width for completed tests
                filled_w = round(
                    completed / total * bar_w
                )
                filled_w = max(1, min(filled_w, bar_w))
                # Ensure at least 1 empty char when not done
                if completed < total and filled_w == bar_w:
                    filled_w = bar_w - 1
                # Distribute within filled_w
                g = max(1, round(
                    passed / completed * filled_w
                )) if passed > 0 else 0
                r = max(1, round(
                    failed / completed * filled_w
                )) if failed > 0 else 0
                sk = max(1, round(
                    skipped / completed * filled_w
                )) if skipped > 0 else 0
                # Fix rounding to exactly filled_w
                used = g + r + sk
                diff = used - filled_w
                if diff != 0:
                    segs = sorted(
                        [('g', g), ('r', r), ('sk', sk)],
                        key=lambda s: -s[1]
                    )
                    for nm, val in segs:
                        if val - diff >= 1:
                            if nm == 'g':
                                g -= diff
                            elif nm == 'r':
                                r -= diff
                            elif nm == 'sk':
                                sk -= diff
                            break
                empty = bar_w - filled_w
            else:
                g = r = sk = 0
                empty = bar_w

            for seg_len, pair in [
                (g, 7), (r, 8), (sk, 9)
            ]:
                if seg_len > 0:
                    self._safe_addstr(
                        stdscr, inner_y + i, cx,
                        " " * seg_len,
                        curses.color_pair(pair)
                    )
                    cx += seg_len
            # Empty portion (not completed yet)
            if empty > 0:
                self._safe_addstr(
                    stdscr, inner_y + i, cx,
                    "░" * empty,
                    curses.color_pair(1) | curses.A_DIM
                )
                cx += empty

            # Stats right-aligned to panel edge
            completed_t = st['completed']
            start_t = st.get('start_time')
            end_t = st.get('end_time')
            if start_t is not None:
                ref = end_t if end_t else datetime.now()
                t_elapsed = _fmt_dur(
                    (ref - start_t).total_seconds()
                )
            else:
                t_elapsed = "0.0s"

            p_str = f"✓{passed:>{max_count_w}}"
            f_str = f"✗{failed:>{max_count_w}}"
            s_str = f"⊘{skipped:>{max_count_w}}"
            c_str = (
                f"{completed_t:>{max_count_w}}"
                f"/{total:>{max_count_w}}"
            )
            t_str = f"{t_elapsed:>{time_w}}"

            right_edge = inner_x + inner_w
            sx = right_edge - stats_w
            self._safe_addstr(
                stdscr, inner_y + i, sx,
                p_str, curses.color_pair(3)
            )
            sx += len(p_str) + 1
            self._safe_addstr(
                stdscr, inner_y + i, sx,
                f_str, curses.color_pair(2)
            )
            sx += len(f_str) + 1
            self._safe_addstr(
                stdscr, inner_y + i, sx,
                s_str, curses.color_pair(4)
            )
            sx += len(s_str) + 1
            self._safe_addstr(
                stdscr, inner_y + i, sx,
                c_str,
                curses.color_pair(1) | curses.A_DIM
            )
            sx += len(c_str) + 1
            self._safe_addstr(
                stdscr, inner_y + i, sx,
                t_str,
                curses.color_pair(1) | curses.A_DIM
            )

    def _draw_progress(
        self, stdscr: Any, y: int, x: int,
        h: int, w: int
    ) -> None:
        """Draw the progress bar."""
        ref = self._end_time or datetime.now()
        elapsed = _fmt_dur(
            (ref - self.start_time).total_seconds()
        )

        # Box
        self._draw_box(
            stdscr, y, x, h, w, "", color=6
        )

        inner_y = y + 1
        inner_x = x + 2
        inner_w = w - 4

        # Compute widths for right-aligned stats
        # Order: ✓P ✗F ⊘S D/T time [quit]
        total_w = len(str(self.total))
        p_str = f"✓{self.passed:>{total_w}}"
        f_str = f"✗{self.failed:>{total_w}}"
        s_str = f"⊘{self.skipped:>{total_w}}"
        count_str = (
            f"{self.completed:>{total_w}}/{self.total}"
        )
        time_w = 6
        elapsed_str = f"{elapsed:>{time_w}}"
        done_str = (
            "  [q/Enter/ESC to quit]"
            if self._done.is_set() else ""
        )
        stats_total_w = (
            len(p_str) + 1
            + len(f_str) + 1
            + len(s_str) + 1
            + len(count_str) + 1
            + len(elapsed_str)
            + len(done_str)
        )

        bar_w = max(5, inner_w - stats_total_w)

        # Compute bar segments
        if self.total > 0 and self.completed > 0:
            filled_w = round(
                self.completed / self.total * bar_w
            )
            filled_w = max(1, min(filled_w, bar_w))
            # Ensure at least 1 empty char when not done
            if (self.completed < self.total
                    and filled_w == bar_w):
                filled_w = bar_w - 1
            g = max(1, round(
                self.passed / self.completed * filled_w
            )) if self.passed > 0 else 0
            r = max(1, round(
                self.failed / self.completed * filled_w
            )) if self.failed > 0 else 0
            sk = max(1, round(
                self.skipped / self.completed * filled_w
            )) if self.skipped > 0 else 0
            used = g + r + sk
            diff = used - filled_w
            if diff != 0:
                segs = sorted(
                    [('g', g), ('r', r), ('sk', sk)],
                    key=lambda s: -s[1]
                )
                for nm, val in segs:
                    if val - diff >= 1:
                        if nm == 'g':
                            g -= diff
                        elif nm == 'r':
                            r -= diff
                        elif nm == 'sk':
                            sk -= diff
                        break
            empty = bar_w - filled_w
        else:
            g = r = sk = 0
            empty = bar_w

        # Draw bar
        bx = inner_x
        for seg_len, pair in [
            (g, 7), (r, 8), (sk, 9)
        ]:
            if seg_len > 0:
                self._safe_addstr(
                    stdscr, inner_y, bx,
                    " " * seg_len,
                    curses.color_pair(pair)
                )
                bx += seg_len
        if empty > 0:
            self._safe_addstr(
                stdscr, inner_y, bx,
                "░" * empty,
                curses.color_pair(1) | curses.A_DIM
            )
            bx += empty

        # Stats right-aligned: ✓P ✗F ⊘S D/T time
        right_edge = inner_x + inner_w
        sx = right_edge - stats_total_w
        self._safe_addstr(
            stdscr, inner_y, sx,
            p_str, curses.color_pair(3)
        )
        sx += len(p_str) + 1
        self._safe_addstr(
            stdscr, inner_y, sx,
            f_str, curses.color_pair(2)
        )
        sx += len(f_str) + 1
        self._safe_addstr(
            stdscr, inner_y, sx,
            s_str, curses.color_pair(4)
        )
        sx += len(s_str) + 1
        self._safe_addstr(
            stdscr, inner_y, sx,
            count_str,
            curses.color_pair(1) | curses.A_DIM
        )
        sx += len(count_str) + 1
        self._safe_addstr(
            stdscr, inner_y, sx,
            elapsed_str,
            curses.color_pair(1) | curses.A_DIM
        )
        if done_str:
            sx += len(elapsed_str)
            self._safe_addstr(
                stdscr, inner_y, sx,
                done_str, curses.color_pair(6)
            )


def run_tui(runner: Any) -> None:
    """Launch the curses TUI."""
    tui = CursesTUI(runner)
    runner.tui = tui
    curses.wrapper(tui.run)
    runner.tui = None
    tui._done.wait(timeout=5)
