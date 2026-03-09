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
Terminal colors and table row formatting for test output.

Uses rich for styled terminal output. The bcolors class is kept for
backward compatibility (used in tests.py for start/end banners).
"""


class bcolors:
    """ANSI color codes for terminal output (legacy, used in test banners)."""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    BG_HEADER =  '\033[105m'
    BG_OKBLUE =  '\033[104m'
    BG_OKGREEN = '\033[102m'
    BG_WARNING = '\033[103m'
    BG_FAIL =    '\033[101m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def table_dump_row(table, name, config, duration, passed, failed, skipped, excluded):
    """Add a row to a rich Table with appropriate styling for failures."""

    total = passed + failed

    skipped_str = str(skipped) if skipped != 0 else ''
    excluded_str = str(excluded) if excluded != 0 else ''

    if failed == 0:
        failed_str = ''
        name_str = name
        config_str = config
        style = None
    else:
        failed_str = str(failed)
        name_str = name
        config_str = config
        style = "red"

    table.add_row(
        name_str, config_str, f"{duration:.2f}",
        '%d/%d' % (passed, total), failed_str,
        skipped_str, excluded_str,
        style=style
    )
