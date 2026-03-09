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

import os

import gvtest.testsuite as testsuite
from gvtest.targets import Target
from gvtest.tests import (
    TestImpl, MakeTestImpl, GvrunTestImpl,
    SdkTestImpl, NetlistPowerSdkTestImpl,
)


class TestsetImpl(testsuite.Testset):

    def __init__(self, runner, target, parent=None, path=None):
        self.runner = runner
        self.name = None
        self.tests = []
        self.testsets = []
        self.parent = parent
        self.path = path
        self.targets = {}
        self.active_targets = []
        self.target = target

    def get_target(self):
        return self.target

    def get_path(self):
        return self.path

    def get_property(self, name):
        return self.runner.get_property(name)

    def get_platform(self):
        return self.runner.get_platform()

    def set_name(self, name):
        self.name = name

    def add_target(self, name, config=None):
        if config is None:
            config = '{}'
        self.targets[name] = Target(name, config)

    def get_full_name(self):
        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                if self.name is None:
                    return parent_name
                else:
                    return f'{parent_name}:{self.name}'

        return self.name

    def import_testset(self, file):
        filepath = file
        if self.path is not None:
            filepath = os.path.join(self.path, file)

        for target in self.__get_targets():
            self.testsets.append(self.runner.import_testset(filepath, target, self))

    def add_testset(self, callback):
        for target in self.__get_targets():
            self.__new_testset(callback, target)

    def __get_targets(self):
        if len(self.targets) == 0:
            targets = [self.target]
        else:
            target_names = self.runner.get_active_targets()
            if len(self.targets) != 0 and len(target_names) == 1 and target_names[0] == 'default':
                target_names = self.targets

            targets = []
            for target_name in target_names:
                target = self.targets.get(target_name)
                if target is not None:
                    targets.append(target)

        return targets

    def __new_testset(self, callback, target):
        testset = TestsetImpl(self.runner, target, self, path=self.path)
        self.testsets.append(testset)
        callback(testset)
        return testset

    def new_testset(self, testset_name):
        testset = TestsetImpl(self.runner, self.target, self, path=self.path)
        testset.set_name(testset_name)
        self.testsets.append(testset)

        return testset

    def dump_tests(self, table, indent='', parent_targets=[]):

        targets = list(self.targets.keys())
        targets += parent_targets

        if self.name is not None:
            table.add_row(indent + self.name, self.get_full_name(), ", ".join(targets))
            indent += '  '

        for testset in self.testsets:
            testset.dump_tests(table, indent, targets)

        if len(self.tests) > 0:
            for test in self.tests:
                test.dump_tests(table, indent, targets)


    def enqueue(self):

        for testset in self.testsets:
            testset.enqueue()

        for test in self.tests:
            test.enqueue()


    def new_test(self, name):
        test = TestImpl(self.runner, self, name, self.target, self.path)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test


    def new_gvrun_test(self, name, flags='', checker=None, retval=0):
        test = GvrunTestImpl(self.runner, self, name, self.target, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_make_test(self, name, flags='', checker=None, retval=0, path=None):
        test = MakeTestImpl(self.runner, self, name, self.target, self.path if path is None else path, flags, checker=checker,
            retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_test(self, name, flags='', checker=None, retval=0):
        test = SdkTestImpl(self.runner, self, name, self.target, self.path, flags, checker=checker, retval=retval)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test

    def new_sdk_netlist_power_test(self, name, flags=''):
        test = NetlistPowerSdkTestImpl(self.runner, self, name, self.target, self.path, flags)
        if self.runner.is_selected(test):
            self.tests.append(test)
        return test
