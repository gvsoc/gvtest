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
from gvtest.container import ContainerConfig
from gvtest.targets import Target
from gvtest.pytest_integration import PytestTestset
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
        self.target: Any | None = target
        self.container: ContainerConfig | None = None

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

    def set_container(
        self,
        image: str | None = None,
        runtime: str = 'docker',
        volumes: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        options: list[str] | None = None,
        setup: str | None = None,
        workdir: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Set a container configuration for this testset.

        All tests in this testset (and nested testsets
        unless they override) will execute inside the
        specified container.

        Can be called with individual parameters::

            testset.set_container(
                image='ghcr.io/org/image:tag',
                setup='pip install -e .',
            )

        Or with a dict (e.g. from gvtest.yaml)::

            testset.set_container(
                config={'image': '...', 'setup': '...'}
            )
        """
        if config is not None:
            self.container = ContainerConfig.from_dict(
                config
            )
        elif image is not None:
            self.container = ContainerConfig(
                image=image,
                runtime=runtime,
                volumes=volumes,
                env=env,
                options=options,
                setup=setup,
                workdir=workdir,
            )
        else:
            raise ValueError(
                "set_container requires 'image' or 'config'"
            )

    def get_container(self) -> ContainerConfig | None:
        """Return the effective container config.

        Walks up the testset hierarchy: the nearest
        ancestor (including self) with a container wins.
        """
        if self.container is not None:
            return self.container
        if self.parent is not None:
            return self.parent.get_container()
        return None

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

        sub_dir = os.path.dirname(filepath)

        if self.runner._has_own_targets(sub_dir):
            # Sub-dir defines its own targets.
            sub_targets = (
                self.runner._resolve_targets_for_dir(sub_dir)
            )
            sub_names = [t.name for t in sub_targets]
            my_name = (
                self.target.name
                if hasattr(self.target, 'name')
                else 'default'
            )

            if my_name == 'default' or my_name in sub_names:
                # First parent to reach here, or parent's
                # target matches: fan out to sub's targets
                for target in sub_targets:
                    self.testsets.append(
                        self.runner.import_testset(
                            filepath, target, self
                        )
                    )
            # else: parent's target not in sub's list → skip
        else:
            # Inherit parent's target
            self.testsets.append(
                self.runner.import_testset(
                    filepath, self.target, self
                )
            )

    def add_testset(self, callback: Callable[[TestsetImpl], None]) -> None:
        # No fan-out here: the testset already has its
        # target assigned by import_testset / add_testset
        # in Runner
        self.__new_testset(callback, self.target)

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

        targets: list[str] = list(parent_targets)

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


    def import_pytest(
        self, path: str, pytest_exe: str = 'pytest'
    ) -> None:
        """Import pytest tests from a directory or file.

        Discovers all pytest tests at build time and creates
        gvtest test entries. At run time, selected tests are
        executed as a single batched pytest invocation.

        Args:
            path: Directory or file containing pytest tests.
            pytest_exe: Pytest executable (default: pytest).
        """
        if not os.path.isabs(path):
            if self.path is not None:
                pytest_path = os.path.join(self.path, path)
            else:
                pytest_path = os.path.join(
                    os.getcwd(), path
                )
        else:
            pytest_path = path

        # Derive a name from the path
        name = os.path.basename(
            pytest_path.rstrip('/')
        )

        pt = PytestTestset(
            self.runner, self, name, self.target,
            os.path.dirname(pytest_path) or os.getcwd(),
            pytest_path, pytest_exe
        )
        pt.discover()
        self.testsets.append(pt)

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
