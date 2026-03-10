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
TestsetImpl — concrete implementation of the Testset abstract class.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from rich.table import Table

import gvtest.testsuite as testsuite
from gvtest.targets import Target
from gvtest.tests import (
    TestCommon, TestImpl, MakeTestImpl, GvrunTestImpl,
    SdkTestImpl, NetlistPowerSdkTestImpl,
)


class TestsetImpl(testsuite.Testset):

    def __init__(
        self, runner: Any, target: Any | None,
        parent: TestsetImpl | None = None,
        path: str | None = None
    ) -> None:
        self.runner: Any = runner
        self.name: str | None = None
        self.tests: list[TestCommon] = []
        self.testsets: list[TestsetImpl] = []
        self.parent: TestsetImpl | None = parent
        self.path: str | None = path
        self.targets: dict[str, Target] = {}
        self.active_targets: list[Target] = []
        self.target: Any | None = target

    def get_target(self) -> Any | None:
        return self.target

    def get_path(self) -> str | None:
        return self.path

    def get_property(self, name: str) -> Any | None:
        return self.runner.get_property(name)

    def get_platform(self) -> str | None:
        return self.runner.get_platform()

    def set_name(self, name: str) -> None:
        self.name = name

    def add_target(self, name: str, config: str | None = None) -> None:
        if config is None:
            config = '{}'
        self.targets[name] = Target(name, config)

    def get_full_name(self) -> str | None:
        if self.parent is not None:
            parent_name: str | None = self.parent.get_full_name()
            if parent_name is not None:
                if self.name is None:
                    return parent_name
                else:
                    return f'{parent_name}:{self.name}'

        return self.name

    def import_testset(self, file: str) -> None:
        filepath: str = file
        if self.path is not None:
            filepath = os.path.join(self.path, file)

        for target in self.__get_targets():
            self.testsets.append(self.runner.import_testset(filepath, target, self))

    def add_testset(self, callback: Callable[[TestsetImpl], None]) -> None:
        for target in self.__get_targets():
            self.__new_testset(callback, target)

    def __get_targets(self) -> list[Any]:
        if len(self.targets) == 0:
            targets: list[Any] = [self.target]
        else:
            active_targets: list[str] = self.runner.get_active_targets()
            if (len(self.targets) != 0
                    and len(active_targets) == 1
                    and active_targets[0] == 'default'):
                target_keys: list[str] = list(self.targets.keys())
            else:
                target_keys = active_targets

            targets = []
            for target_name in target_keys:
                target: Target | None = self.targets.get(target_name)
                if target is not None:
                    targets.append(target)

        return targets

    def __new_testset(self, callback: Callable[[TestsetImpl], None], target: Any) -> TestsetImpl:
        testset: TestsetImpl = TestsetImpl(self.runner, target, self, path=self.path)
        self.testsets.append(testset)
        callback(testset)
        return testset

    def new_testset(self, testset_name: str) -> TestsetImpl:
        testset: TestsetImpl = TestsetImpl(self.runner, self.target, self, path=self.path)
        testset.set_name(testset_name)
        self.testsets.append(testset)

        return testset

    def dump_tests(self, table: Table, indent: str = '', parent_targets: list[str] = []) -> None:

        targets: list[str] = list(self.targets.keys())
        targets += parent_targets

        if self.name is not None:
            table.add_row(indent + self.name, self.get_full_name(), ", ".join(targets))
            indent += '  '

        for testset in self.testsets:
            testset.dump_tests(table, indent, targets)

        if len(self.tests) > 0:
            for test in self.tests:
                test.dump_tests(table, indent, targets)


    def enqueue(self) -> None:

        for testset in self.testsets:
            testset.enqueue()

        for test in self.tests:
            test.enqueue()


    def new_test(self, name: str) -> TestImpl:
        test: TestImpl = TestImpl(self.runner, self, name, self.target, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_gvrun_test(
        self, name: str, flags: str = '',
        checker: Callable[..., Any] | None = None,
        retval: int = 0
    ) -> GvrunTestImpl:
        test: GvrunTestImpl = GvrunTestImpl(
            self.runner, self, name, self.target,
            self.path, flags, checker=checker,
            retval=retval
        )
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_make_test(
        self, name: str, flags: str = '',
        checker: Callable[..., Any] | None = None,
        retval: int = 0, path: str | None = None
    ) -> MakeTestImpl:
        test: MakeTestImpl = MakeTestImpl(
            self.runner, self, name, self.target,
            self.path if path is None else path,
            flags, checker=checker, retval=retval
        )
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_test(
        self, name: str, flags: str | None = None,
        checker: Callable[..., Any] | None = None,
        retval: int = 0
    ) -> SdkTestImpl:
        test: SdkTestImpl = SdkTestImpl(
            self.runner, self, name, self.target,
            self.path, flags, checker=checker,
            retval=retval
        )
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_netlist_power_test(
        self, name: str, flags: str | None = None
    ) -> NetlistPowerSdkTestImpl:
        test: NetlistPowerSdkTestImpl = (
            NetlistPowerSdkTestImpl(
                self.runner, self, name,
                self.target, self.path, flags
            )
        )
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test
