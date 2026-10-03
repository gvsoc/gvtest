"""
Tests for the run listeners (gvtest.events), Runner.rerun() and the web GUI
server (gvtest.gui).
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from gvtest.runner import Runner
from gvtest.events import RunListener
import gvtest.gui
from gvtest.gui import WebGui, run_gui


TESTSET = '''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('suite')
    ok = testset.new_test('ok')
    ok.add_command(Shell('run', 'echo hello; echo "cycles: 42"'))
    ok.add_bench('cycles', r'cycles: (\\d+)', 'Cycles')
    flaky = testset.new_test('flaky')
    flaky.add_command(Shell('run', 'test -e %(marker)s'))
    testset.new_test('later').skip('not yet')
'''


class Recorder(RunListener):
    """Records the events as (event, test name, status)."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def _record(self, event, run=None):
        with self.lock:
            self.events.append((
                event,
                run.test.get_full_name() if run is not None else None,
                run.status if run is not None else None,
            ))

    def test_counted(self, run):
        self._record('counted', run)

    def set_total(self, total):
        with self.lock:
            self.events.append(('total', total, None))

    def test_started(self, run):
        self._record('started', run)

    def test_finished(self, run):
        self._record('finished', run)

    def run_finished(self):
        self._record('run_finished')

    def of(self, name):
        return [(e, s) for e, n, s in self.events if n == name]


@pytest.fixture
def make_runner(tmp_path):
    runners = []

    def _make(content=TESTSET, **kwargs):
        marker = tmp_path / 'marker'
        testset_file = tmp_path / 'testset.cfg'
        testset_file.write_text(content % {'marker': marker})
        defaults = {'properties': [], 'flags': [], 'nb_threads': 2,
                    'progress': False}
        defaults.update(kwargs)
        runner = Runner(**defaults)
        runner.add_testset(str(testset_file))
        runner.marker = marker
        runners.append(runner)
        return runner

    yield _make

    for runner in runners:
        runner.stop()


def run_of(runner, name):
    for test in runner.testsets[0].tests:
        if test.name == name:
            return test.runs[0]
    raise KeyError(name)


class TestListeners:

    def test_events_of_a_run(self, make_runner):
        runner = make_runner()
        recorder = Recorder()
        runner.add_listener(recorder)
        runner.start()
        runner.run()

        assert recorder.of('suite:ok') == [
            ('counted', 'failed'), ('started', 'failed'),
            ('finished', 'passed')]
        assert recorder.of('suite:flaky')[-1] == ('finished', 'failed')
        # A skipped test is counted and finished, never started
        assert recorder.of('suite:later') == [
            ('counted', 'failed'), ('finished', 'skipped')]
        assert ('total', 3, None) in recorder.events
        assert recorder.events[-1][0] == 'run_finished'

    def test_terminal_output_is_a_listener_of_the_pass(self, make_runner):
        runner = make_runner()
        runner.start()
        runner.run()
        # The console output is only registered while the pass runs
        assert runner.listeners == []

    def test_no_terminal_output_when_a_listener_owns_it(self, make_runner):
        class Owner(RunListener):
            owns_terminal = True

        runner = make_runner()
        runner.add_listener(Owner())
        assert runner._start_display() is None


class TestRerun:

    def test_rerun_replaces_the_run(self, make_runner):
        runner = make_runner()
        runner.start()
        runner.run()
        assert runner.stats.stats['failed'] == 1
        first = run_of(runner, 'flaky')

        runner.marker.write_text('')
        recorder = Recorder()
        runner.add_listener(recorder)
        runner.rerun([first])

        second = run_of(runner, 'flaky')
        assert second is not first
        assert second.status == 'passed'
        assert runner.stats.stats['failed'] == 0
        assert runner.stats.stats['passed'] == 2
        assert recorder.of('suite:flaky') == [
            ('counted', 'failed'), ('started', 'failed'),
            ('finished', 'passed')]
        assert ('total', 1, None) in recorder.events

    def test_rerun_keeps_one_bench_result_per_run(self, make_runner):
        runner = make_runner()
        runner.start()
        runner.run()
        runner.rerun([run_of(runner, 'ok')])
        results = [r for r in runner.bench_results
                   if r['test'] == 'suite:ok']
        assert len(results) == 1
        assert results[0]['value'] == 42

    def test_interrupted_rerun_keeps_the_earlier_result(self, make_runner):
        runner = make_runner()
        runner.start()
        runner.run()
        first = run_of(runner, 'ok')
        assert first.status == 'passed'

        # Interrupt as soon as the rerun is enqueued, before it starts
        class Interrupter(RunListener):
            def set_total(self, total):
                runner.interrupt()

        runner.add_listener(Interrupter())
        runner.rerun([first])

        assert run_of(runner, 'ok') is first
        assert runner.stats.stats['passed'] == 1
        assert len([r for r in runner.bench_results
                    if r['test'] == 'suite:ok']) == 1


@pytest.fixture
def gui(make_runner):
    runner = make_runner()
    gui = WebGui(runner, '127.0.0.1', 0)
    runner.add_listener(gui)
    gui.serve()
    runner.start()
    gui.begin_pass()
    runner.run()
    yield gui
    gui.close()


def request(gui, path, body=None, token=True):
    url = f'http://127.0.0.1:{gui.server.server_address[1]}{path}'
    headers = {'X-Gvtest-Token': gui.token} if token else {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read().decode()


def row_of(gui, name):
    return next(r for r in gui.rows if r['name'] == name)


class TestWebGui:

    def test_rows_follow_the_run(self, gui):
        assert gui.state == 'idle'
        assert row_of(gui, 'suite:ok')['status'] == 'passed'
        assert row_of(gui, 'suite:flaky')['status'] == 'failed'
        assert row_of(gui, 'suite:later')['status'] == 'skipped'
        # Every result is dated, to list them in the order they came
        assert all(r['finished'] is not None for r in gui.rows)

    def test_page_is_served(self, gui):
        assert '<title>gvtest</title>' in request(gui, '/', token=False)

    def test_api_needs_the_token(self, gui):
        with pytest.raises(urllib.error.HTTPError) as error:
            request(gui, '/api/run?id=0', token=False)
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            request(gui, '/api/stop', body={}, token=False)
        assert error.value.code == 403

    def test_events_start_with_a_snapshot(self, gui):
        url = (f'http://127.0.0.1:{gui.server.server_address[1]}'
               f'/api/events?token={gui.token}')
        with urllib.request.urlopen(url, timeout=5) as response:
            line = response.readline().decode()
        assert line.startswith('data: ')
        snapshot = json.loads(line[len('data: '):])
        assert snapshot['t'] == 'snapshot'
        assert snapshot['state'] == 'idle'
        assert {r['name'] for r in snapshot['rows']} == {
            'suite:ok', 'suite:flaky', 'suite:later'}

    def test_run_details(self, gui):
        row = row_of(gui, 'suite:ok')
        details = json.loads(request(gui, f'/api/run?id={row["id"]}'))
        assert details['status'] == 'passed'
        assert 'hello' in details['output']
        assert details['offset'] == len(details['output'])
        assert details['commands'][0]['kind'] == 'shell'
        assert details['benchs'][0]['metric'] == 'cycles'
        assert details['benchs'][0]['value'] == 42

        skipped = row_of(gui, 'suite:later')
        details = json.loads(request(gui, f'/api/run?id={skipped["id"]}'))
        assert details['skip_message'] == 'not yet'

    def test_tail_of_a_finished_run_ends_at_once(self, gui):
        row = row_of(gui, 'suite:ok')
        stream = request(gui, f'/api/tail?id={row["id"]}&offset=0')
        messages = [json.loads(line[len('data: '):])
                    for line in stream.splitlines()
                    if line.startswith('data: ')]
        assert 'hello' in messages[0]['text']
        assert messages[-1] == {'end': True}

    def test_rerun_is_queued_for_the_main_thread(self, gui):
        row = row_of(gui, 'suite:flaky')
        skipped = row_of(gui, 'suite:later')
        request(gui, '/api/rerun', body={'ids': [row['id'], skipped['id']]})
        # A skipped test is not run again
        assert gui.commands.get(timeout=5) == ('rerun', [row['id']])

        # The pass is taken: a second request is refused
        with pytest.raises(urllib.error.HTTPError) as error:
            request(gui, '/api/rerun', body={'ids': [row['id']]})
        assert error.value.code == 409

        gui.runner.marker.write_text('')
        gui.begin_pass()
        gui.runner.rerun([gui.runs[row['id']]])
        assert gui.state == 'idle'
        assert row_of(gui, 'suite:flaky')['status'] == 'passed'
        assert row_of(gui, 'suite:flaky')['id'] == row['id']

    def test_stopped_rerun_shows_the_earlier_result(self, gui):
        runner = gui.runner
        row = row_of(gui, 'suite:ok')
        finished = row['finished']

        class Interrupter(RunListener):
            def set_total(self, total):
                runner.interrupt()

        runner.add_listener(Interrupter())
        gui.begin_pass()
        runner.rerun([gui.runs[row['id']]])
        assert row_of(gui, 'suite:ok')['status'] == 'passed'
        assert row_of(gui, 'suite:ok')['finished'] == finished

    def test_quit_is_queued(self, gui):
        request(gui, '/api/quit', body={})
        assert gui.commands.get(timeout=5) == ('quit',)


class TestGuiExit:
    """run_gui(exit_unwatched=True), as a CI job runs it."""

    def test_token_given(self, make_runner):
        gui = WebGui(make_runner(), '127.0.0.1', 0, token='build-12')
        assert gui.url.endswith('/?token=build-12')
        gui.server.server_close()

    def test_leaves_when_no_page_is_open(self, make_runner):
        runner = make_runner()
        runner.start()
        start = time.monotonic()
        run_gui(runner, '127.0.0.1', 0, exit_unwatched=True)
        assert time.monotonic() - start < 10
        assert runner.stats.stats['passed'] == 1

    def test_waits_for_the_open_page(self, make_runner, monkeypatch):
        monkeypatch.setattr(gvtest.gui, '_UNWATCHED_S', 0.5)
        runner = make_runner()
        runner.start()
        guis = []

        class Gui(WebGui):
            def __init__(self, *args):
                super().__init__(*args)
                guis.append(self)
                # A page open from the start
                self.page = self.subscribe()[1]

        monkeypatch.setattr(gvtest.gui, 'WebGui', Gui)
        thread = threading.Thread(
            target=run_gui, args=(runner, '127.0.0.1', 0, None, True))
        thread.start()
        time.sleep(3)
        assert thread.is_alive()
        assert guis[0].state == 'idle'
        guis[0].unsubscribe(guis[0].page)
        thread.join(timeout=10)
        assert not thread.is_alive()
