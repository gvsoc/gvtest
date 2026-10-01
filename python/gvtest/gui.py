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
Web GUI for gvtest, launched with --gui.

gvtest serves one page (gui.html) from a small HTTP server living in its
own process, and the page follows the run in a browser:

    GET  /              the page
    GET  /api/events    server-sent events: a snapshot, then every change
    GET  /api/run       one run in detail: commands, benchmarks, output
    GET  /api/tail      server-sent events: the output of a running test
    POST /api/rerun     run again the given runs (only between two passes)
    POST /api/stop      interrupt the pass
    POST /api/quit      leave gvtest

Every /api request carries the token printed with the URL. The server
threads only read the runner state and queue commands; the tests are
always run from the main thread, by run_gui().
"""

from __future__ import annotations

import errno
import json
import os
import queue
import secrets
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any
from urllib.parse import urlparse, parse_qs

from rich.console import Console

import gvtest.testsuite as testsuite
from gvtest.events import RunListener

DEFAULT_PORT = 8730

# The page gets at most this much of the end of an output in one go
_MAX_OUTPUT_CHARS = 1_000_000

# A silent event stream is pinged this often, which is also how a closed
# browser tab gets noticed
_KEEPALIVE_S = 15

# Statuses a run can be started again from
_RERUNNABLE = ('passed', 'failed', 'cancelled')


class WebGui(RunListener):
    """Follows the run as a listener and serves it over HTTP."""

    def __init__(self, runner: Any, host: str, port: int | None) -> None:
        self.runner: Any = runner
        self.lock: threading.Lock = threading.Lock()
        self.token: str = secrets.token_urlsafe(8)
        # One row per (test, target); a rerun reuses the row of the run
        # it replaces, so the ids the page holds stay valid.
        self.rows: list[dict[str, Any]] = []
        self.runs: list[Any] = []
        self._row_ids: dict[tuple[str, str], int] = {}
        self.state: str = 'starting'
        self.pass_started: float | None = None
        self.pass_ended: float | None = None
        self._clients: list[queue.SimpleQueue[str | None]] = []
        # Row id -> events of the /api/tail streams waiting for output
        self._tails: dict[int, list[threading.Event]] = {}
        # Commands for the main thread: ('rerun', ids) or ('quit',).
        # A SimpleQueue because the SIGINT handler puts into it.
        self.commands: queue.SimpleQueue[tuple[Any, ...]] = \
            queue.SimpleQueue()

        self.server: ThreadingHTTPServer = self._bind(host, port)
        self.server.daemon_threads = True
        self.server.gui = self  # type: ignore[attr-defined]
        shown_host: str = host
        if host in ('', '0.0.0.0', '::'):
            shown_host = socket.getfqdn()
        self.url: str = (
            f'http://{shown_host}:{self.server.server_address[1]}'
            f'/?token={self.token}'
        )

    @staticmethod
    def _bind(host: str, port: int | None) -> ThreadingHTTPServer:
        if port is not None:
            return ThreadingHTTPServer((host, port), _Handler)
        try:
            return ThreadingHTTPServer((host, DEFAULT_PORT), _Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            # Another gvtest is serving there: take any free port
            return ThreadingHTTPServer((host, 0), _Handler)

    def serve(self) -> None:
        threading.Thread(
            target=self.server.serve_forever, daemon=True
        ).start()

    def close(self) -> None:
        with self.lock:
            self.state = 'exited'
            self._broadcast({'t': 'bye'})
            for client in self._clients:
                client.put(None)
            for events in self._tails.values():
                for event in events:
                    event.set()
        self.server.shutdown()
        self.server.server_close()

    # ---- run events ---------------------------------------------------

    def test_counted(self, run: Any) -> None:
        key = (run.test.get_full_name() or '', run.config)
        with self.lock:
            row_id = self._row_ids.get(key)
            if row_id is None:
                row_id = len(self.rows)
                self._row_ids[key] = row_id
                self.rows.append({
                    'id': row_id, 'name': key[0], 'target': key[1],
                })
                self.runs.append(run)
            else:
                self.runs[row_id] = run
            run.gui_id = row_id
            row = self.rows[row_id]
            row.update(
                status='pending', started=None, duration=0, finished=None
            )
            self._broadcast({'t': 'row', 'row': row})
            self._wake_tails(row_id)

    def test_started(self, run: Any) -> None:
        self._set_row(run, status='running', started=time.time())

    def test_finished(self, run: Any) -> None:
        status: str = run.status
        if status == 'failed' and not run.started:
            # Dropped by an interrupt; 'failed' is only its initial status
            status = 'cancelled'
        # Kept on the run: one restored after an interrupted rerun stays
        # where it finished in the order of the results
        if getattr(run, 'gui_finished', None) is None:
            run.gui_finished = time.time()
        self._set_row(
            run, status=status, duration=run.duration,
            finished=run.gui_finished
        )

    def test_updated(self, run: Any) -> None:
        self._set_row(run, status=run.status, duration=run.duration)

    def test_output(self, run: Any) -> None:
        # Called for every output line of every test: stay cheap when
        # nobody is watching this one
        events = self._tails.get(getattr(run, 'gui_id', -1))
        if events:
            for event in list(events):
                event.set()

    def run_finished(self) -> None:
        with self.lock:
            # What an interrupt left unstarted never gets a status
            for row in self.rows:
                if row['status'] in ('pending', 'running'):
                    row['status'] = 'cancelled'
                    self._broadcast({'t': 'row', 'row': row})
                    self._wake_tails(row['id'])
            self.state = 'idle'
            self.pass_ended = time.time()
            self._broadcast(self._state_msg())

    def begin_pass(self) -> None:
        with self.lock:
            self.state = 'running'
            self.pass_started = time.time()
            self.pass_ended = None
            self._broadcast(self._state_msg())

    def _set_row(self, run: Any, **fields: Any) -> None:
        with self.lock:
            row_id = getattr(run, 'gui_id', None)
            if row_id is None or self.runs[row_id] is not run:
                return
            row = self.rows[row_id]
            row.update(fields)
            self._broadcast({'t': 'row', 'row': row})
            self._wake_tails(row_id)

    def _wake_tails(self, row_id: int) -> None:
        for event in self._tails.get(row_id, ()):
            event.set()

    def _state_msg(self) -> dict[str, Any]:
        return {
            't': 'state', 'state': self.state, 'now': time.time(),
            'started': self.pass_started, 'ended': self.pass_ended,
        }

    def _broadcast(self, msg: dict[str, Any]) -> None:
        """Caller must hold self.lock, so that every client sees the
        changes in the order they happened."""
        data: str = json.dumps(msg)
        for client in self._clients:
            client.put(data)

    # ---- what the request handlers use --------------------------------

    def subscribe(self) -> tuple[str, queue.SimpleQueue[str | None]]:
        """The current state, and a queue getting every later change."""
        client: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        with self.lock:
            snapshot = dict(self._state_msg())
            snapshot.update(
                t='snapshot', rows=self.rows,
                title=self._title(), threads=self.runner.nb_threads,
            )
            data = json.dumps(snapshot)
            self._clients.append(client)
        return data, client

    def unsubscribe(self, client: queue.SimpleQueue[str | None]) -> None:
        with self.lock:
            if client in self._clients:
                self._clients.remove(client)

    def _title(self) -> str:
        names = [t.name for t in self.runner.testsets
                 if getattr(t, 'name', None)]
        return names[0] if names else os.path.basename(os.getcwd())

    def get_run(self, row_id: int) -> tuple[dict[str, Any], Any] | None:
        with self.lock:
            if not 0 <= row_id < len(self.rows):
                return None
            return dict(self.rows[row_id]), self.runs[row_id]

    def details(self, row_id: int) -> dict[str, Any] | None:
        found = self.get_run(row_id)
        if found is None:
            return None
        row, run = found
        test = run.test

        commands: list[dict[str, str]] = []
        for command in test.commands:
            name = getattr(command, 'name', '') or ''
            if isinstance(command, testsuite.Shell):
                cmd = command.cmd
                if run.target is not None:
                    try:
                        cmd = run.target.format_properties(cmd)
                    except (KeyError, IndexError, ValueError):
                        # Shown as declared; running it reports the error
                        pass
                commands.append({'name': name, 'kind': 'shell', 'cmd': cmd})
            elif isinstance(command, testsuite.Checker):
                commands.append({'name': name, 'kind': 'checker', 'cmd': ''})
            else:
                commands.append({'name': name, 'kind': 'call', 'cmd': ''})

        benchs: list[dict[str, Any]] = [
            {k: r.get(k) for k in (
                'metric', 'value', 'ref', 'tol', 'ref_type', 'description')}
            for r in list(self.runner.bench_results)
            if r['test'] == row['name'] and r['target'] == row['target']
        ]

        output: str = run.output
        total: int = len(output)
        row.update(
            path=test.path, sourceme=run.sourceme,
            node_id=getattr(test, 'node_id', None),
            description=test.description or '',
            skip_message=run.skip_message,
            timeout=run.timeout_reached,
            commands=commands, benchs=benchs,
            output=output[-_MAX_OUTPUT_CHARS:], offset=total,
            hidden=max(0, total - _MAX_OUTPUT_CHARS),
            now=time.time(),
        )
        return row

    def add_tail(self, row_id: int) -> threading.Event:
        event = threading.Event()
        with self.lock:
            self._tails.setdefault(row_id, []).append(event)
        return event

    def remove_tail(self, row_id: int, event: threading.Event) -> None:
        with self.lock:
            events = self._tails.get(row_id, [])
            if event in events:
                events.remove(event)
            if not events:
                self._tails.pop(row_id, None)

    def request_rerun(self, ids: list[int]) -> str | None:
        """Queue a rerun for the main thread; returns why it can't."""
        with self.lock:
            if self.state != 'idle':
                return 'a run is in progress'
            ids = [
                i for i in ids
                if isinstance(i, int) and 0 <= i < len(self.rows)
                and self.rows[i]['status'] in _RERUNNABLE
            ]
            if not ids:
                return 'nothing to run again'
            # Taken now, so that a second request is refused
            self.state = 'running'
        self.commands.put(('rerun', ids))
        return None

    def request_stop(self) -> None:
        if self.state == 'running':
            self.runner.interrupt()

    def request_quit(self) -> None:
        self.request_stop()
        self.commands.put(('quit',))


class _Handler(BaseHTTPRequestHandler):

    server_version = 'gvtest'

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @property
    def gui(self) -> WebGui:
        return self.server.gui  # type: ignore[attr-defined]

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.headers.get('X-Gvtest-Token') or \
            (query.get('token') or [''])[0]
        return secrets.compare_digest(
            token.encode(), self.gui.token.encode())

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), 'application/json')

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def _stream(self, data: str) -> None:
        self.wfile.write(f'data: {data}\n\n'.encode())
        self.wfile.flush()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == '/':
            page = os.path.join(os.path.dirname(__file__), 'gui.html')
            with open(page, 'rb') as file:
                self._send(200, file.read(), 'text/html; charset=utf-8')
            return

        if not url.path.startswith('/api/'):
            self._send(404, b'Not found', 'text/plain')
            return
        if not self._authorized(query):
            self._send(403, b'Bad token', 'text/plain')
            return

        try:
            if url.path == '/api/events':
                self._events()
            elif url.path == '/api/run':
                details = self.gui.details(int(query['id'][0]))
                if details is None:
                    self._send(404, b'No such run', 'text/plain')
                else:
                    self._send_json(details)
            elif url.path == '/api/tail':
                self._tail(int(query['id'][0]), int(query['offset'][0]))
            else:
                self._send(404, b'Not found', 'text/plain')
        except (KeyError, ValueError):
            self._send(400, b'Bad request', 'text/plain')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            self._send(403, b'Bad token', 'text/plain')
            return

        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            self._send(400, b'Bad request', 'text/plain')
            return

        if url.path == '/api/rerun':
            error = self.gui.request_rerun(body.get('ids') or [])
            if error is not None:
                self._send_json({'error': error}, 409)
                return
        elif url.path == '/api/stop':
            self.gui.request_stop()
        elif url.path == '/api/quit':
            self.gui.request_quit()
        else:
            self._send(404, b'Not found', 'text/plain')
            return
        self._send_json({})

    def _events(self) -> None:
        snapshot, client = self.gui.subscribe()
        try:
            self._start_stream()
            self._stream(snapshot)
            while True:
                try:
                    data = client.get(timeout=_KEEPALIVE_S)
                except queue.Empty:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                if data is None:
                    break
                self._stream(data)
        finally:
            self.gui.unsubscribe(client)

    def _tail(self, row_id: int, offset: int) -> None:
        """Stream what a run outputs from `offset`, until it is over."""
        found = self.gui.get_run(row_id)
        if found is None:
            self._send(404, b'No such run', 'text/plain')
            return
        run = found[1]
        event = self.gui.add_tail(row_id)
        try:
            self._start_stream()
            while True:
                event.clear()
                # Read the state before the output: what is appended
                # after this point sets the event again
                current = self.gui.get_run(row_id)
                over = (
                    current is None or current[1] is not run
                    or current[0]['status'] not in ('pending', 'running')
                    or self.gui.state == 'exited'
                )
                output: str = run.output
                if len(output) > offset:
                    self._stream(json.dumps({'text': output[offset:]}))
                    offset = len(output)
                if over:
                    self._stream(json.dumps({'end': True}))
                    break
                if not event.wait(timeout=_KEEPALIVE_S):
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        finally:
            self.gui.remove_tail(row_id, event)


def run_gui(runner: Any, host: str, port: int | None) -> None:
    """Run the tests while serving the GUI, then keep serving it, running
    again what the page asks for, until the page or Ctrl+C says quit."""
    gui = WebGui(runner, host, port)
    console = Console(highlight=False, stderr=True)

    def announce(text: str) -> None:
        console.print(
            f'{text} [link={gui.url}]{gui.url}[/link]', soft_wrap=True
        )

    busy: bool = True

    def on_sigint(signum: int, frame: FrameType | None) -> None:
        if busy:
            runner._handle_interrupt(signum, frame)
        else:
            gui.commands.put(('quit',))

    runner.add_listener(gui)
    gui.serve()
    announce('GUI:')
    on_main_thread = \
        threading.current_thread() is threading.main_thread()
    if on_main_thread:
        prev_sigint = signal.signal(signal.SIGINT, on_sigint)

    try:
        gui.begin_pass()
        runner.run()
        while True:
            busy = False
            announce('Done. Ctrl+C to quit, GUI still at')
            command = gui.commands.get()
            if command[0] == 'quit':
                break
            busy = True
            runs = [gui.runs[i] for i in command[1]]
            gui.begin_pass()
            runner.rerun(runs)
    finally:
        if on_main_thread:
            signal.signal(signal.SIGINT, prev_sigint)
        runner.remove_listener(gui)
        gui.close()
