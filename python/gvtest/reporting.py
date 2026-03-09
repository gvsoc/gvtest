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
Table row formatting for test output using rich.
"""


def table_dump_row(table, name, config, duration, passed, failed, skipped, excluded):
    """Add a row to a rich Table with appropriate styling for failures."""

    total = passed + failed

    skipped_str = str(skipped) if skipped != 0 else ''
    excluded_str = str(excluded) if excluded != 0 else ''

    if failed == 0:
        failed_str = ''
        style = None
    else:
        failed_str = str(failed)
        style = "red"

    table.add_row(
        name, config, f"{duration:.2f}",
        '%d/%d' % (passed, total), failed_str,
        skipped_str, excluded_str,
        style=style
    )
