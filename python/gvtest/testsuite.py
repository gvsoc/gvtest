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

from __future__ import annotations

import abc
from typing import Any, Callable


class Target(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def get_name(self) -> str: pass


class Command: pass

class Shell(Command):

  def __init__(self, name: str, cmd: str, retval: int = 0) -> None:
    self.name: str = name
    self.cmd: str = cmd
    self.retval: int = retval


class Call(Command):

  def __init__(self, name: str, callback: Callable[[], int]) -> None:
    self.name: str = name
    self.callback: Callable[[], int] = callback


class Checker(Command):

  def __init__(self, name: str, callback: Callable[..., Any], *kargs: Any, **kwargs: Any) -> None:
    self.name: str = name
    self.callback: tuple[
        Callable[..., Any], tuple[Any, ...],
        dict[str, Any]
    ] = callback, kargs, kwargs


class Test(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(self, extract: str, name: str, desc: str) -> None: pass

    @abc.abstractmethod
    def get_path(self) -> str: pass

    @abc.abstractmethod
    def add_command(self, command: Command) -> None: pass



class Testset(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def set_name(self, name: str) -> None: pass

    @abc.abstractmethod
    def add_target(self, name: str, config: str | None) -> None: pass

    @abc.abstractmethod
    def get_target(self) -> Target | None: pass

    @abc.abstractmethod
    def import_testset(self, file: str) -> None: pass

    @abc.abstractmethod
    def new_testset(self, testset_name: str) -> Testset: pass

    @abc.abstractmethod
    def new_test(self, name: str) -> Test: pass

    @abc.abstractmethod
    def new_sdk_test(self, name: str, flags: str | None=None) -> SdkTest: pass

    @abc.abstractmethod
    def new_sdk_netlist_power_test(self, name: str, flags: str | None = None) -> SdkTest: pass

    @abc.abstractmethod
    def get_property(self, name: str) -> Any: pass

    @abc.abstractmethod
    def get_platform(self) -> str | None: pass

    @abc.abstractmethod
    def get_path(self) -> str | None: pass



class SdkTest(object, metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def add_bench(self, extract: str, name: str, desc: str) -> None: pass
