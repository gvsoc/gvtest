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
Target configuration — platform targets with properties, env vars, and sourceme scripts.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Optional


class Target(object):

    def __init__(self, name: str, config: str | None = None) -> None:
        self.name: str = name
        if config is None:
            config = '{}'
        self.config: dict[str, Any] = json.loads(config)

    def get_name(self) -> str:
        return self.name

    def get_sourceme(self) -> str | None:
        sourceme = self.config.get('sourceme')

        if sourceme is not None:
            return ast.literal_eval(sourceme)

        return None

    def get_envvars(self) -> dict[str, str] | None:
        envvars = self.config.get('envvars')

        if envvars is not None:
            result: dict[str, str] = {}
            for key, value in envvars.items():
                try:
                    eval_value = ast.literal_eval(value)
                    if eval_value is None:
                        eval_value = ""
                    result[key] = eval_value
                except:
                    result[key] = ""
            return result

        return None

    def format_properties(self, str: str) -> str:
        properties = self.config.get('properties')
        if properties is None:
            return str
        return str.format(**properties)

    def get_property(self, name: str) -> Any | None:
        properties = self.config.get('properties')
        if properties is None:
            return None
        return properties.get(name)
