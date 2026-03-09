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

import psutil

import gvtest.testsuite as testsuite
from gvtest.reporting import bcolors


class TestRun(object):

    def __init__(self, test, target):
        self.target = target
        self.test = test
        self.runner = test.runner
        self.lock = threading.Lock()
        self.duration = 0
        if target is not None:
            self.config = target.name
        else:
            self.config = self.runner.config

        self.sourceme = None
        self.envvars = None

        if self.target is not None:
            self.sourceme = self.target.get_sourceme()
            self.envvars = self.target.get_envvars()

    def get_target_name(self):
        if self.target is None:
            return self.config

        return self.target.name

    def get_stats(self, stats):
        stats[self.status] += 1
        stats['duration'] = self.duration

    # Called by worker to execute the test
    def run(self):

        self.__print_start_message()

        self.output = ''
        self.status = "passed"

        start_time = datetime.now()

        timeout = self.runner.max_timeout
        self.timeout_reached = False

        if timeout != -1:
            timer = Timer(timeout, self.kill)
            timer.start()

        for command in self.test.commands:

            retval = self.__exec_command(command, self.target, self.sourceme, self.envvars)

            if retval != 0 or self.timeout_reached:
                if self.timeout_reached:
                    self.__dump_test_msg('--- Timeout reached ---\n')
                self.status = "failed"
                break

        if timeout != -1:
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

    def kill(self):
        self.lock.acquire()
        self.timeout_reached = True
        if self.current_proc is not None:
            try:
                process = psutil.Process(pid=self.current_proc.pid)

                for children in process.children(recursive=True):
                    os.kill(children.pid, signal.SIGKILL)
            except:
                pass
        self.lock.release()

    # Print start bannier
    def __print_start_message(self):
        testname = self.test.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config = self.target.name
        else:
            config = self.runner.get_config()
        print (bcolors.OKBLUE + 'START'.ljust(8) + bcolors.ENDC + bcolors.BOLD + testname + bcolors.ENDC + ' %s' % (config))
        sys.stdout.flush()

    # Print end bannier
    def print_end_message(self):
        testname = self.test.get_full_name().ljust(self.runner.get_max_testname_len() + 5)
        if self.target is not None:
            config = self.target.name
        else:
            config = self.runner.get_config()

        if self.status == 'passed':
            test_result_str = bcolors.OKGREEN + 'OK '.ljust(8) + bcolors.ENDC
        elif self.status == 'failed':
            test_result_str = bcolors.FAIL + 'KO '.ljust(8) + bcolors.ENDC
        elif self.status == 'skipped':
            test_result_str = bcolors.WARNING + 'SKIP '.ljust(8) + bcolors.ENDC
        elif self.status == 'excluded':
            test_result_str = bcolors.HEADER + 'EXCLUDE '.ljust(8) + bcolors.ENDC

        print (test_result_str + bcolors.BOLD + testname + bcolors.ENDC + ' %s' % (config))
        sys.stdout.flush()

    def __exec_process(self, command, envvars=None):
        self.lock.acquire()
        if self.timeout_reached:
            return ['', -1]

        env = os.environ.copy()

        if envvars is not None:
            env.update(envvars)

        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True,
            cwd=self.test.path, env=env)

        self.current_proc = proc

        self.lock.release()

        for line in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors='replace'):
            self.__dump_test_msg(line)

        retval = proc.wait()
        self.current_proc = None

        return retval


    def __dump_test_msg(self, msg):
        self.output += msg
        if self.runner.stdout:
            print (msg[:-1])


    # Called by run method to execute specific command
    def __exec_command(self, command, target, sourceme, envvars):

        if type(command) == testsuite.Shell:
            cmd = command.cmd
            if self.target is not None:
                cmd = self.target.format_properties(cmd)

            self.__dump_test_msg(f'--- Shell command: {cmd} ---\n')

            if sourceme is not None:
                cmd = f'gvtest_cmd_stub {sourceme} {cmd}'

            retval = 0 if self.__exec_process(cmd, envvars) == command.retval else 1

        elif type(command) == testsuite.Checker:
            self.__dump_test_msg(f'--- Checker command ---\n')
            try:
                result = command.callback[0](self, self.output, *command.callback[1], **command.callback[2])
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

    def __init__(self, runner, parent, name, target, path):
        self.runner = runner
        self.target = target
        self.name = name
        self.parent = parent
        self.full_name = None
        self.commands = []
        self.path = path
        self.status = None
        self.skipped = None
        self.description = None
        if self.path == '':
            self.path = os.getcwd()
        self.current_proc = None

        self.full_name = self.name

        if self.parent is not None:
            parent_name = self.parent.get_full_name()
            if parent_name is not None:
                self.full_name =  f'{parent_name}:{self.name}'

        self.runner.declare_name(self.full_name)
        self.benchs = []
        self.runs = []

    def skip(self, msg):
        self.skipped = msg
        return self

    def get_target(self):
        return self.target

    # Called by user to add commands
    def add_command(self, command):
        self.commands.append(command)


    # Called by runner to enqueue this test to the list of tests ready to be executed
    def enqueue(self):
        run = TestRun(self, self.target)
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

    def dump_tests(self, table, indent, targets):
        table.add_row(indent + self.name, self.get_full_name(), ", ".join(targets))

    # Can be called to get full name including hierarchy path
    def get_full_name(self):
        return self.full_name

    def get_path(self):
        return self.full_name.replace(':', '/')

    def add_description(self, description):
        self.description = description


class TestImpl(TestCommon, testsuite.Test):

    def __init__(self, runner, parent, name, target, path):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class MakeTestImpl(TestCommon, testsuite.Test):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' platform=%s' % platform

        workdir = os.environ.get('GVSOC_WORKDIR')
        if workdir is None:
            builddir = f'{path}/build/{runner.get_config()}/{self.name}'
        else:
            builddir = f'{workdir}/tests/{self.get_path()}'
        self.flags += f' build={builddir}'

        self.add_command(testsuite.Shell('clean', 'make clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'make build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'make run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class GvrunTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        target = target.get_name()

        workdir = os.environ.get('GVSOC_WORKDIR')
        if workdir is None:
            builddir = f'build/{target}/{self.name}'
        else:
            builddir = f'{workdir}/tests/{self.get_path()}/{target}'
        self.flags += f' --work-dir={builddir}'

        cmd = f'gvrun --target {target} {self.flags}'
        self.add_command(testsuite.Shell('clean', f'{cmd} clean'))
        self.add_command(testsuite.Shell('build', f'{cmd} build'))
        self.add_command(testsuite.Shell('run', f'{cmd} run', retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class SdkTestImpl(testsuite.SdkTest, TestCommon):

    def __init__(self, runner, parent, name, target, path, flags, checker=None, retval=0):
        TestCommon.__init__(self, runner, parent, name, target, path)
        self.runner = runner
        self.name = name
        self.flags = flags
        if self.flags is not None:
            self.flags += ' ' + ' '.join(self.runner.flags)
        else:
            self.flags = ' '.join(self.runner.flags)

        platform = self.runner.get_property('platform')
        if platform is not None:
            self.flags += ' --platform=%s' % platform

        self.flags += f' --build=build/{runner.get_config()}/{self.name}'

        self.add_command(testsuite.Shell('clean', 'posbuild clean %s' % (self.flags)))
        self.add_command(testsuite.Shell('build', 'posbuild build %s' % (self.flags)))
        self.add_command(testsuite.Shell('run', 'posbuild run %s' % (self.flags), retval=retval))

        if checker is not None:
            self.add_command(testsuite.Checker('check', checker))

    def add_bench(self, extract, name, desc):
        self.benchs.append([extract, name, desc])


class NetlistPowerSdkTestImpl(SdkTestImpl):

    def __init__(self, runner, parent, name, target, path, flags):
        SdkTestImpl.__init__(self, runner, parent, name, target, path, flags)

        self.add_command(testsuite.Shell('power_gen', 'make power_gen %s' % (self.flags)))
        self.add_command(testsuite.Shell('power_copy', 'make power_copy %s' % (self.flags)))
