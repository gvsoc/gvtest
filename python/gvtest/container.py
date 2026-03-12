#!/usr/bin/env python3

#
# Copyright (C) 2025 ETH Zurich, University of Bologna
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
Container execution backend for gvtest.

Wraps shell commands in ``docker run`` (or ``podman run``)
invocations so that tests execute inside a container while
operating on the host filesystem via transparent bind mounts.

The container sees the same absolute paths as the host, so
build artifacts, test outputs, and logs are directly
accessible from either side.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)


class ContainerConfig:
    """Immutable container execution configuration.

    Attributes:
        image: Container image name (e.g.
            ``ghcr.io/pulp-platform/deeploy:devel``).
        runtime: Container runtime command
            (``docker`` or ``podman``). Default: ``docker``.
        volumes: Extra bind-mount mappings
            ``{host_path: container_path}``.
            The test working directory is always mounted
            transparently (same path) and does not need
            to be listed here.
        env: Extra environment variables passed to the
            container.
        options: Additional flags forwarded verbatim to
            ``docker run`` (e.g. ``['--gpus', 'all']``).
        setup: Optional shell snippet executed inside the
            container before the actual command
            (e.g. ``pip install -e .``).
        workdir: Override the working directory inside the
            container. When *None* the test's own ``path``
            is used (which is already bind-mounted
            transparently).
    """

    def __init__(
        self,
        image: str,
        runtime: str = 'docker',
        volumes: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        options: list[str] | None = None,
        setup: str | None = None,
        workdir: str | None = None,
    ) -> None:
        self.image: str = image
        self.runtime: str = runtime
        self.volumes: dict[str, str] = dict(
            volumes or {}
        )
        self.env: dict[str, str] = dict(env or {})
        self.options: list[str] = list(options or [])
        self.setup: str | None = setup
        self.workdir: str | None = workdir

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContainerConfig:
        """Create a ContainerConfig from a parsed YAML/dict.

        Expected keys (all optional except ``image``):

        .. code-block:: yaml

            container:
              image: ghcr.io/org/image:tag
              runtime: docker          # or podman
              volumes:
                /host/path: /container/path
              env:
                KEY: value
              options:
                - --gpus
                - all
              setup: pip install -e .
              workdir: /app
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"container config must be a mapping, "
                f"got {type(data).__name__}"
            )
        image = data.get('image')
        if not image:
            raise ValueError(
                "container config requires 'image'"
            )
        return cls(
            image=image,
            runtime=data.get('runtime', 'docker'),
            volumes=data.get('volumes'),
            env=data.get('env'),
            options=data.get('options'),
            setup=data.get('setup'),
            workdir=data.get('workdir'),
        )

    def build_run_cmd(
        self,
        inner_cmd: str,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        extra_volumes: dict[str, str] | None = None,
    ) -> list[str]:
        """Build a full ``docker run`` command list.

        Args:
            inner_cmd: The shell command to run inside
                the container.
            cwd: Working directory on the host. Mounted
                transparently and used as ``-w``.
            extra_env: Per-invocation env vars (merged
                with config-level env).
            extra_volumes: Per-invocation extra mounts.

        Returns:
            Command list suitable for ``subprocess.Popen``.
        """
        cmd: list[str] = [self.runtime, 'run', '--rm']

        # ── Transparent mount of cwd ───────────────────
        effective_cwd = cwd or os.getcwd()
        effective_cwd = os.path.realpath(effective_cwd)
        mounted_paths: set[str] = set()

        cmd += ['-v', f'{effective_cwd}:{effective_cwd}']
        mounted_paths.add(effective_cwd)

        # ── Explicit volumes ───────────────────────────
        all_volumes = dict(self.volumes)
        if extra_volumes:
            all_volumes.update(extra_volumes)

        for host_path, container_path in all_volumes.items():
            host_real = os.path.realpath(host_path)
            # Skip if already covered by the cwd mount
            if host_real == effective_cwd:
                continue
            # Skip if it's a sub-path of cwd (already visible)
            if host_real.startswith(effective_cwd + '/'):
                continue
            if host_real not in mounted_paths:
                cmd += [
                    '-v',
                    f'{host_real}:{container_path}'
                ]
                mounted_paths.add(host_real)

        # ── Working directory ──────────────────────────
        workdir = self.workdir or effective_cwd
        cmd += ['-w', workdir]

        # ── Environment variables ──────────────────────
        all_env = dict(self.env)
        if extra_env:
            all_env.update(extra_env)
        for key, value in all_env.items():
            cmd += ['-e', f'{key}={value}']

        # ── Extra options ──────────────────────────────
        cmd += self.options

        # ── Image ──────────────────────────────────────
        cmd.append(self.image)

        # ── Inner command ──────────────────────────────
        if self.setup:
            full_cmd = f'{self.setup} && {inner_cmd}'
        else:
            full_cmd = inner_cmd

        cmd += ['bash', '-c', full_cmd]

        return cmd

    def validate(self) -> None:
        """Check that the container runtime is available.

        Raises:
            RuntimeError: If the runtime binary is not
                found in ``$PATH``.
        """
        if shutil.which(self.runtime) is None:
            raise RuntimeError(
                f"Container runtime '{self.runtime}' "
                f"not found in PATH"
            )

    def __repr__(self) -> str:
        return (
            f"ContainerConfig(image={self.image!r}, "
            f"runtime={self.runtime!r})"
        )
