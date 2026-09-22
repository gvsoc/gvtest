"""
Tests for gvtest.bench — DB schema/migration and the calibration report.
"""

import html
import json
import re
import sqlite3

import pytest

from gvtest.bench.db import init_db, insert_json, _SCHEMA_VERSION
from gvtest.bench import calibration


# ---------------------------------------------------------------------------
# DB schema migration
# ---------------------------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    git_commit TEXT,
    git_branch TEXT,
    platform   TEXT NOT NULL,
    json_file  TEXT
);
CREATE TABLE results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    test        TEXT NOT NULL,
    target      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    description TEXT,
    UNIQUE(run_id, test, target, metric)
);
"""


class TestDbMigration:

    def test_legacy_db_gains_reference_columns(self, tmp_path):
        db_path = str(tmp_path / 'bench.sqlite')
        conn = sqlite3.connect(db_path)
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO runs (timestamp, platform) VALUES ('t0', 'gvsoc')")
        conn.execute(
            "INSERT INTO results (run_id, test, target, metric, value) "
            "VALUES (1, 'a', 'default', 'cycles', 42)")
        conn.commit()
        conn.close()

        conn = init_db(db_path)
        row = conn.execute(
            "SELECT value, reference, tolerance, ref_type, "
            "value_min, value_max FROM results").fetchone()
        assert row == (42.0, None, None, None, None, None)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()

        # Idempotent on re-open
        conn = init_db(db_path)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()

    def test_fresh_db_at_current_version(self, tmp_path):
        conn = init_db(str(tmp_path / 'bench.sqlite'))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        assert {'reference', 'tolerance', 'ref_type',
                'value_min', 'value_max', 'kind', 'better'} <= cols
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()

    def test_v3_db_gains_builds_and_run_columns(self, tmp_path):
        db_path = str(tmp_path / 'bench.sqlite')
        conn = sqlite3.connect(db_path)
        conn.executescript(_LEGACY_SCHEMA)
        for col, decl in (('reference', 'REAL'), ('tolerance', 'REAL'),
                          ('ref_type', 'TEXT'),
                          ('value_min', 'REAL'), ('value_max', 'REAL')):
            conn.execute(f"ALTER TABLE results ADD COLUMN {col} {decl}")
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO runs (timestamp, platform) VALUES ('t0', 'gvsoc')")
        conn.commit()
        conn.close()

        conn = init_db(db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'builds' in tables
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert {'build_id', 'uuid', 'uploaded_at'} <= cols
        cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        assert {'kind', 'better'} <= cols       # v5
        assert conn.execute(
            "SELECT build_id, uuid, uploaded_at FROM runs").fetchone() == \
            (None, None, None)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION

        # uuid uniqueness is enforced, but only on non-NULL values
        conn.execute("INSERT INTO runs (timestamp, platform, uuid) "
                     "VALUES ('t1', 'gvsoc', 'u1')")
        conn.execute("INSERT INTO runs (timestamp, platform) "
                     "VALUES ('t2', 'gvsoc')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO runs (timestamp, platform, uuid) "
                         "VALUES ('t3', 'gvsoc', 'u1')")
        conn.close()

        # Idempotent on re-open
        conn = init_db(db_path)
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        conn.close()


# ---------------------------------------------------------------------------
# What a metric is measured for: kind and direction
# ---------------------------------------------------------------------------

class TestBenchKind:

    def _test(self, tmp_path):
        from gvtest.runner import Runner
        cfg = tmp_path / 'testset.cfg'
        cfg.write_text('''
from gvtest.testsuite import *

def testset_build(testset):
    testset.set_name('app')
    test = testset.new_test('fir')
    test.add_command(Shell('run', 'echo "cycles: 42"'))
    test.set_bench_kind('benchmark')
    test.add_bench('fir.cycles', r'cycles: (\\d+)', 'Cycles of one frame')
    test.add_bench('fir.bw', r'cycles: (\\d+)', 'Throughput',
                   better='higher')
''')
        r = Runner(properties=[], flags=[], nb_threads=1)
        r.add_testset(str(cfg))
        return r

    def test_kind_and_direction_reach_the_results(self, tmp_path):
        r = self._test(tmp_path)
        r.start()
        r.run()
        r.stop()
        by_metric = {b['metric']: b for b in r.bench_results}
        # the test's kind applies to both metrics, the direction is per metric
        assert by_metric['fir.cycles']['kind'] == 'benchmark'
        assert by_metric['fir.cycles']['better'] == 'lower'
        assert by_metric['fir.bw']['better'] == 'higher'

    def test_unknown_kind_or_direction_is_refused(self):
        from gvtest.testsuite import Bench
        with pytest.raises(ValueError, match='unknown kind'):
            Bench.make('m', 'r', kind='perf')
        with pytest.raises(ValueError, match='better must be'):
            Bench.make('m', 'r', better='bigger')

    def test_db_keeps_kind_and_direction(self, tmp_path):
        db = _make_db(tmp_path, [_run([
            {**_result('app:fir', 'tgt', 'fir.cycles', 42),
             'kind': 'benchmark', 'better': 'lower'},
            _result('el:dma', 'tgt', 'dma.cycles', 100)])])
        conn = sqlite3.connect(db)
        assert sorted(conn.execute(
            'SELECT metric, kind, better FROM results')) == [
            ('dma.cycles', None, 'lower'), ('fir.cycles', 'benchmark', 'lower')]
        conn.close()

    def test_tag_existing_results(self, tmp_path):
        from gvtest.bench.db import tag
        db = _make_db(tmp_path, [_run([
            _result('pulpos:bench:events', 'tgt', 'cycles', 42),
            _result('el:dma', 'tgt', 'dma.cycles', 100)])])
        # A test that becomes a benchmark leaves its history untagged
        assert tag(db, 'pulpos:bench:*', 'benchmark') == 1      # dry run
        conn = sqlite3.connect(db)
        assert conn.execute('SELECT COUNT(*) FROM results WHERE kind IS NOT '
                            'NULL').fetchone()[0] == 0
        conn.close()
        assert tag(db, 'pulpos:bench:*', 'benchmark', better='lower',
                   apply=True) == 1
        conn = sqlite3.connect(db)
        assert sorted(conn.execute('SELECT test, kind FROM results')) == [
            ('el:dma', None), ('pulpos:bench:events', 'benchmark')]
        conn.close()


# ---------------------------------------------------------------------------
# Selecting the runs a report is about: branch, CI job
# ---------------------------------------------------------------------------

class TestRunSelection:

    def _db(self, tmp_path):
        """Two runs on main and one on a branch, interleaved in time."""
        db = str(tmp_path / 'bench.sqlite')
        conn = init_db(db)
        rows = (('main', 'sdk', 110), ('my-work', 'sdk-branch', 102),
                ('main', 'sdk', 112))
        for i, (branch, job, value) in enumerate(rows):
            build = conn.execute(
                'INSERT INTO builds (job, build_number, timestamp) '
                'VALUES (?, ?, ?)', (job, i, f'2026-07-{16 + i}')).lastrowid
            run = conn.execute(
                'INSERT INTO runs (timestamp, git_commit, git_branch, '
                'platform, build_id) VALUES (?, ?, ?, ?, ?)',
                (f'2026-07-{16 + i}T10:00:00+00:00', f'c{i}', branch,
                 'gvsoc', build)).lastrowid
            conn.execute(
                'INSERT INTO results (run_id, test, target, metric, value, '
                'reference, ref_type) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (run, 't:a', 'tgt', 'm', value, 100, 'rtl'))
        conn.commit()
        conn.close()
        return db

    def test_latest_result_per_selection(self, tmp_path):
        conn = sqlite3.connect(self._db(tmp_path))
        key = ('t:a', 'tgt', 'm')
        # Without a filter the branch run can be the latest one seen
        assert calibration.query_results(conn)[key]['value'] == 112
        assert calibration.query_results(
            conn, branch='my-work')[key]['value'] == 102
        assert calibration.query_results(
            conn, branch='main')[key]['value'] == 112
        # Runs recorded before branches were say 'HEAD': select by CI job
        assert calibration.query_results(
            conn, job='sdk-branch')[key]['value'] == 102
        conn.close()

    def test_history_of_one_branch_only(self, tmp_path):
        conn = sqlite3.connect(self._db(tmp_path))
        hist = calibration.build_history(
            calibration.query_history(conn, branch='main'))
        points = hist['metrics'][('t:a', 'tgt', 'm')]
        assert [p['value'] for p in points] == [110, 112]   # no branch run
        assert len(calibration.build_history(
            calibration.query_history(conn))['runs']) == 3
        conn.close()


class TestBranchRecording:

    def test_env_branch_used_when_git_is_detached(self, monkeypatch):
        from gvtest.runner import Runner
        r = Runner(properties=[], flags=[])
        monkeypatch.setattr(r, '_get_git_info', lambda *a: 'HEAD')
        for var in ('GVTEST_BENCH_BRANCH', 'GIT_BRANCH', 'BRANCH_NAME',
                    'CI_COMMIT_REF_NAME'):
            monkeypatch.delenv(var, raising=False)
        assert r._git_branch() == 'HEAD'
        monkeypatch.setenv('GIT_BRANCH', 'origin/my-work')
        assert r._git_branch() == 'my-work'      # remote prefix dropped
        monkeypatch.setenv('GVTEST_BENCH_BRANCH', 'chosen')
        assert r._git_branch() == 'chosen'

    def test_git_branch_wins_when_checked_out(self, monkeypatch):
        from gvtest.runner import Runner
        r = Runner(properties=[], flags=[])
        monkeypatch.setattr(r, '_get_git_info', lambda *a: 'local-branch')
        monkeypatch.setenv('GIT_BRANCH', 'origin/other')
        assert r._git_branch() == 'local-branch'


# ---------------------------------------------------------------------------
# Renaming recorded results after a testset rename
# ---------------------------------------------------------------------------

class TestRename:

    def _db(self, tmp_path):
        return _make_db(tmp_path, [_run([
            _result('el:dma_l1_l2', 'tgt', 'dma_bw.priv.ext2loc.2d', 3.1),
            _result('el:dma_l1_l2', 'tgt', 'dma_bw.ext.ext2loc.2d', 5.2),
            _result('other', 'tgt', 'm', 1.0)])])

    def _names(self, db):
        conn = sqlite3.connect(db)
        names = sorted(conn.execute('SELECT test, metric FROM results'))
        conn.close()
        return names

    def test_rename_test(self, tmp_path):
        from gvtest.bench.db import rename
        db = self._db(tmp_path)
        assert rename(db, 'el:dma_l1_l2', 'el:dma:dma_l1_mem') == 2
        assert self._names(db)[0][0] == 'el:dma_l1_l2'   # dry run by default
        assert rename(db, 'el:dma_l1_l2', 'el:dma:dma_l1_mem', apply=True) == 2
        assert self._names(db) == [
            ('el:dma:dma_l1_mem', 'dma_bw.ext.ext2loc.2d'),
            ('el:dma:dma_l1_mem', 'dma_bw.priv.ext2loc.2d'),
            ('other', 'm')]

    def test_rename_test_and_metric(self, tmp_path):
        from gvtest.bench.db import rename
        db = self._db(tmp_path)
        assert rename(db, 'el:dma_l1_l2', 'el:dma_ext_ram',
                      metric_re=r'^dma_bw\.ext\.(\w+)\.(.*)$',
                      metric_sub=r'extram_bw.extram_\1_\2', apply=True) == 2
        assert ('el:dma_ext_ram', 'extram_bw.extram_ext2loc_2d') \
            in self._names(db)

    def test_rename_refuses_collisions(self, tmp_path, capsys):
        from gvtest.bench.db import rename
        db = self._db(tmp_path)
        # renaming onto a name the same run already holds
        assert rename(db, 'el:dma_l1_l2', 'el:dma_l1_l2',
                      metric_re=r'^dma_bw\.\w+\.', metric_sub='dma_bw.priv.',
                      apply=True) == -1
        assert 'collide' in capsys.readouterr().err
        assert len(self._names(db)) == 3        # nothing written

    def test_rename_nothing_to_do(self, tmp_path):
        from gvtest.bench.db import rename
        assert rename(self._db(tmp_path), 'el:absent', 'el:other') == 0


# ---------------------------------------------------------------------------
# Trend report rendering
# ---------------------------------------------------------------------------

class TestReportRender:

    _TRENDS = {'t:a': {'grp.cycles': {'desc': 'd', 'targets': {
        'tgt': {'timestamps': ['2026-07-16T10:00:00+00:00'],
                'values': [1.0], 'commits': ['c0ffee0']}}}}}

    def test_render_to_string_injects_plotly_src_and_subtitle(self):
        from gvtest.bench.report import render_report_html
        html = render_report_html(self._TRENDS,
                                  plotly_src='/static/plotly.min.js',
                                  subtitle='bench server')
        assert '"/static/plotly.min.js"' in html
        assert 'bench server' in html
        assert 'const DATA' in html

    def test_render_default_uses_cdn(self):
        from gvtest.bench.report import render_report_html
        html = render_report_html(self._TRENDS)
        assert 'https://cdn.plot.ly/plotly-2.35.2.min.js' in html
        assert 'Generated by gvtest' in html
        assert '{{' not in html


# ---------------------------------------------------------------------------
# Benchmark descriptions: summary inline, details on demand
# ---------------------------------------------------------------------------

_LONG_DESC = ('Cycles of one 4 KB copy. The core programs the copy and polls '
              'its end. The reference is the RTL.')


class TestDescriptions:

    def test_split_description(self):
        from gvtest.bench import split_description
        assert split_description(_LONG_DESC) == (
            'Cycles of one 4 KB copy.',
            'The core programs the copy and polls its end. The reference is '
            'the RTL.')
        assert split_description('cluster DMA priv (cycles)') == \
            ('cluster DMA priv (cycles)', '')
        assert split_description(None) == ('', '')

    def _model(self, tmp_path, desc):
        db = _make_db(tmp_path, [_run([
            {**_result('t:a', 'tgt', 'long', 110, ref=100, src='rtl'),
             'description': desc},
            _result('t:a', 'tgt', 'short', 100, ref=100, src='rtl')])])
        conn = sqlite3.connect(db)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        return model

    def test_calibration_cell_summary_and_details(self, tmp_path):
        html_str = calibration.render_html(
            self._model(tmp_path, _LONG_DESC), 'descs')
        # Long description: summary in the row, the rest behind a <details>
        assert ('<details class="mdoc"><summary><span class="mname" '
                'title="long">long</span><span class="mdesc">Cycles of one '
                '4 KB copy.</span></summary><p class="mfull">metric: '
                '<code>t:a:long</code></p>'
                '<p>The core programs the copy and polls its end. '
                'The reference is the RTL.</p></details>') in html_str
        # One-sentence description: the row still opens, onto its names
        assert ('<span class="mname" title="short">short</span>'
                '<span class="mdesc">short</span></summary>'
                '<p class="mfull">metric: <code>t:a:short</code></p>'
                ) in html_str
        assert 'class="docs-all"' in html_str

    def test_rows_open_onto_their_full_names(self, tmp_path):
        # Even with nothing else to show, a row opens onto the names to copy
        html_str = calibration.render_html(
            self._model(tmp_path, 'Just a summary'), 'descs')
        assert 'class="docs-all"' in html_str
        assert ('<p class="mfull">metric: <code>t:a:long</code></p>'
                '</details>') in html_str

    def test_trend_report_shows_summary(self):
        from gvtest.bench.report import render_report_html
        trends = {'t:a': {'grp.cycles': {'desc': _LONG_DESC, 'targets': {
            'tgt': {'timestamps': ['2026-07-16T10:00:00+00:00'],
                    'values': [1.0], 'commits': ['c0ffee0']}}}}}
        html_str = render_report_html(trends)
        assert (f'<td title="{html.escape(_LONG_DESC)}">Cycles of one 4 KB '
                'copy.</td>') in html_str


# ---------------------------------------------------------------------------
# Calibration table as a tree: test, then the dotted parts of the metric name
# ---------------------------------------------------------------------------

class TestCalibrationTree:

    def _model(self, tmp_path, runs=1):
        # Δ: a.b.x +10%, a.b.y -30%, a.c.z +2% (ref 100); a second test with
        # one metric. The second run moves a.b.x to +20%.
        db = _make_db(tmp_path, [_run([
            _result('t:one', 'tgt', 'a.b.x', 110 + 10 * i, ref=100, src='rtl'),
            _result('t:one', 'tgt', 'a.b.y', 70, ref=100, src='rtl'),
            _result('t:one', 'tgt', 'a.c.z', 102, ref=100, src='rtl'),
            _result('t:two', 'tgt', 'solo', 100, ref=100, src='rtl')],
            timestamp=f'2026-07-{16 + i}T10:00:00+00:00')
            for i in range(runs)])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        return model, hist

    def _groups(self, html_str):
        """[(gid, key, ancestors, label, meta, 'avg x%', 'max y%')]"""
        out = []
        for gid, key, anc, label, meta, cell in re.findall(
                r'<tr class="grow" data-gid="(\d+)" data-key="([^"]*)" '
                r'data-anc="([^"]*)">.*?<button[^>]*>([^<]*)</button>'
                r'<span class="gmeta">([^<]*)</span></td>'
                r'<td class="num grp stack">(.*?)</td>', html_str):
            figures = re.findall(r'<div class="lv[^"]*"[^>]*>([^<]*)</div>',
                                 cell)
            out.append((gid, key, anc, label, meta, *figures))
        return out

    def test_levels_and_averages(self, tmp_path):
        model, hist = self._model(tmp_path)
        html_str = calibration.render_html(model, 'tree', history=hist)
        groups = {g[3]: (g[0], g[2], g[4], g[5], g[6])
                  for g in self._groups(html_str)}
        # The test names open one level per colon-separated part, and the
        # single-child chain one > a is merged into one level
        assert set(groups) == {'t', 'one › a', 'b', 'c', 'two'}
        root = groups['t']
        top = groups['one › a']
        assert root[1] == ''                          # a first-level row
        assert top[1] == root[0]                      # the test sits under it
        assert groups['two'][1] == root[0]
        assert groups['b'][1] == f'{root[0]} {top[0]}'  # b sits under both
        assert root[3:] == ('avg 10.5%',              # (10 + 30 + 2 + 0) / 4
                            'max 30.0%')             # the worst of the four
        assert top[2] == '3 metrics · 33% within tolerance · worst: b.y'
        assert top[3:] == ('avg 14.0%', 'max 30.0%')  # (10 + 30 + 2) / 3
        assert groups['b'][3:] == ('avg 20.0%', 'max 30.0%')
        assert groups['two'][3:] == ('avg 0.0%', 'max 0.0%')
        # Leaves hang below every level above them
        assert re.search(r'<tr data-anc="%s %s %s"><td class="txt" '
                         r'style="padding-left:[0-9]+px"><details class="mdoc">'
                         r'<summary><span class="mname" '
                         r'title="a.b.x">x</span>'
                         % (root[0], top[0], groups['b'][0]), html_str)
        assert 'class="tree-all" data-open="1"' in html_str

    def test_level_history_is_mean_then_max(self, tmp_path):
        model, hist = self._model(tmp_path, runs=2)
        html_str = calibration.render_html(model, 'tree', history=hist)
        m = re.search(r'<button type="button" class="gt" aria-expanded="true">'
                      r'b</button>.*?<td class="dh stack">(.*?)</td>',
                      html_str, re.S)
        series = [json.loads(html.unescape(d)) for d in
                  re.findall(r'<div class="sp" data-h="([^"]*)"', m.group(1))]
        assert len(series) == 2
        # mean |Δ| then max |Δ|, over the two runs
        assert [p[0] for p in series[0]] == [20.0, 25.0]
        assert [p[0] for p in series[1]] == [30.0, 30.0]
        assert [p[1] for p in series[1]] == ['bad', 'bad']

    def test_value_chart_in_description(self, tmp_path):
        # a.b.x has a one-sentence description (its name), but a history:
        # it still opens, onto the chart of its value against the reference.
        model, hist = self._model(tmp_path, runs=2)
        html_str = calibration.render_html(model, 'tree', history=hist)
        m = re.search(r'title="a\.b\.x">x</span><span class="mdesc">a\.b\.x'
                      r'</span></summary><p class="mfull">metric: '
                      r'<code>t:one:a\.b\.x</code></p>'
                      r'<div class="vchart" data-v="([^"]*)" '
                      r'data-ref="RTL"></div></details>', html_str)
        values = json.loads(html.unescape(m.group(1)))
        assert values == [[110.0, 100.0, 1], [120.0, 100.0, 2]]
        assert "det.querySelector('.vchart')" in html_str   # the drawing


# ---------------------------------------------------------------------------
# Bench server upload client
# ---------------------------------------------------------------------------

class _StubBenchServer:
    """Minimal /api/runs endpoint capturing payloads.

    fail_first makes the first request return 500 (to exercise the retry).
    """

    def __init__(self, fail_first=False):
        import http.server
        import threading
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                stub.requests.append(json.loads(body))
                if stub.fail_first and len(stub.requests) == 1:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(201)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(
                    {'run_id': 1, 'build_id': None, 'duplicate': False,
                     'results': len(stub.requests[-1]['results'])}).encode())

            def log_message(self, *args):
                pass

        self.requests = []
        self.fail_first = fail_first
        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestUpload:

    _ENVELOPE = {'timestamp': 't0', 'git_commit': 'c0ffee0',
                 'git_branch': 'main', 'platform': 'gvsoc',
                 'results': [{'test': 't:a', 'target': 'tgt',
                              'metric': 'cycles', 'value': 42}]}

    def test_post_run_payload(self):
        from gvtest.bench.upload import post_run
        server = _StubBenchServer()
        try:
            result = post_run(server.url, self._ENVELOPE, run_uuid='u-1',
                              build={'job': 'nightly', 'build_number': 3})
            assert result['results'] == 1
            payload = server.requests[0]
            assert payload['run_uuid'] == 'u-1'
            assert payload['build'] == {'job': 'nightly', 'build_number': 3}
            assert payload['results'] == self._ENVELOPE['results']
            assert 'run_uuid' not in self._ENVELOPE  # input not mutated
        finally:
            server.close()

    def test_post_run_retries_on_500(self):
        from gvtest.bench.upload import post_run
        server = _StubBenchServer(fail_first=True)
        try:
            result = post_run(server.url, self._ENVELOPE, run_uuid='u-1')
            assert result['run_id'] == 1
            assert len(server.requests) == 2
        finally:
            server.close()

    def test_post_run_raises_on_dead_server(self):
        from gvtest.bench.upload import post_run, UploadError
        with pytest.raises(UploadError):
            post_run('http://127.0.0.1:1', self._ENVELOPE, run_uuid='u-1')

    def test_parse_build(self):
        from gvtest.bench.upload import parse_build
        assert parse_build('sdk-nightly:42') == \
            {'job': 'sdk-nightly', 'build_number': 42}
        with pytest.raises(ValueError):
            parse_build('no-number')

    def test_runner_upload_failure_is_not_fatal(self):
        from gvtest.runner import Runner
        r = Runner(properties=[], flags=[])
        r.bench_url = 'http://127.0.0.1:1'
        r.bench_build = 'nightly:3'
        # Must warn and return, never raise
        r._upload_bench(self._ENVELOPE)


# ---------------------------------------------------------------------------
# Calibration report
# ---------------------------------------------------------------------------

def _result(test, target, metric, value, ref=None, tol=None, src=None,
            value_min=None, value_max=None):
    return {'test': test, 'target': target, 'metric': metric, 'value': value,
            'description': metric, 'ref': ref, 'tol': tol, 'ref_type': src,
            'value_min': value_min, 'value_max': value_max}


def _make_db(tmp_path, runs):
    db_path = str(tmp_path / 'bench.sqlite')
    for i, run in enumerate(runs):
        json_path = tmp_path / f'run{i}.json'
        json_path.write_text(json.dumps(run))
        insert_json(db_path, str(json_path))
    return db_path


def _run(results, platform='gvsoc', timestamp='2026-07-16T10:00:00+00:00'):
    return {'timestamp': timestamp, 'git_commit': 'c0ffee0',
            'git_branch': 'main', 'platform': platform, 'results': results}


@pytest.fixture
def db_path(tmp_path):
    return _make_db(tmp_path, [_run([
        # ok (within tol), warn (<= 2x tol), bad (> 2x tol)
        _result('t:a', 'tgt1', 'm_ok', 102, ref=100, tol=5, src='rtl'),
        _result('t:a', 'tgt1', 'm_warn', 108, ref=100, tol=5, src='rtl'),
        _result('t:a', 'tgt1', 'm_bad', 150, ref=100, tol=5, src='rtl'),
        # no declared tolerance: default_tol_pct fallback
        _result('t:a', 'tgt1', 'm_notol', 104, ref=100, src='analytical'),
        # ref == 0 with absolute tolerance
        _result('t:a', 'tgt1', 'm_zero', 1, ref=0, tol=2, src='analytical'),
        # measured-only
        _result('t:a', 'tgt1', 'm_free', 7),
        # shared test on sibling targets
        _result('s:shared', 'sib1', 'cycles', 100, ref=100, tol=5, src='rtl'),
        _result('s:shared', 'sib2', 'cycles', 90, ref=100, tol=5, src='rtl'),
        # coverage-gap target: measured values, no references
        _result('c:t', 'gapless', 'lat', 12),
    ])])


class TestCalibrationModel:

    def test_severity_bands(self, db_path):
        conn = sqlite3.connect(db_path)
        rows = calibration.query_results(conn)
        conn.close()
        model = calibration.build_model(rows)
        severity = {c['metric']: c['severity'] for c in model['cells']
                    if c['target'] == 'tgt1'}
        assert severity == {'m_ok': 'ok', 'm_warn': 'warn', 'm_bad': 'bad',
                            'm_notol': 'ok', 'm_zero': 'ok', 'm_free': None}

    def test_one_section_per_target(self, db_path):
        # Targets sharing a test (sib1/sib2) each get their own section
        # rather than being merged into side-by-side columns.
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        assert {tuple(c['targets']) for c in model['clusters']} == \
            {('tgt1',), ('sib1',), ('sib2',), ('gapless',)}
        for cluster in model['clusters']:
            for row in cluster['rows']:
                assert len(row['cells']) == 1
        sib1 = next(c for c in model['clusters'] if c['targets'] == ['sib1'])
        sib2 = next(c for c in model['clusters'] if c['targets'] == ['sib2'])
        assert sib1['rows'][0]['cells'][0]['severity'] == 'ok'
        # 90 vs 100 +/- 5 sits exactly on the 2x-tolerance boundary -> warn
        assert sib2['rows'][0]['cells'][0]['severity'] == 'warn'

    def test_aggregates(self, db_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        g = model['global']
        assert g['n_total'] == 9
        assert g['n_referenced'] == 7
        assert g['n_ok'] == 4  # m_ok, m_notol, m_zero, sib1
        gapless = next(t for t in model['targets']
                       if t['target'] == 'gapless')
        assert gapless['n_referenced'] == 0

    def test_latest_run_wins(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100, ref=100, tol=5)],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 200, ref=100, tol=5)],
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        conn.close()
        assert rows[('t:a', 'tgt', 'm')]['value'] == 200

    def test_baseline_mode(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100)], platform='rtl',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 112)], platform='gvsoc',
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        rows = calibration.query_results(conn, exclude_platform='rtl')
        conn.close()
        model = calibration.build_model(
            rows, baseline=baseline, baseline_label='platform:rtl',
            default_tol_pct=5.0)
        cell = model['cells'][0]
        assert cell['ref'] == 100
        assert cell['ref_type'] == 'platform:rtl'
        assert cell['severity'] == 'bad'  # +12% vs 5% default bands
        assert model['global']['n_referenced'] == 1

    def test_baseline_carries_spread(self, tmp_path):
        # A metric sampled over several activations: the headline value is
        # the average and min/max come along from both sides.
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'filter.f', 59.0,
                          value_min=50, value_max=70)], platform='rtl',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'filter.f', 50.0,
                          value_min=40, value_max=90)], platform='gvsoc',
                 timestamp='2026-07-16T11:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        rows = calibration.query_results(conn, exclude_platform='rtl')
        conn.close()
        model = calibration.build_model(
            rows, baseline=baseline, baseline_label='platform:rtl')
        cell = model['cells'][0]
        assert (cell['value'], cell['value_min'], cell['value_max']) == \
            (50.0, 40, 90)
        assert (cell['ref'], cell['ref_min'], cell['ref_max']) == (59.0, 50, 70)
        # Deltas: headline avg plus each spread bound against its own ref
        assert round(cell['delta_pct'], 1) == -15.3        # 50 vs 59
        assert round(cell['delta_min_pct'], 1) == -20.0    # 40 vs 50
        assert round(cell['delta_max_pct'], 1) == 28.6     # 90 vs 70
        # Only the headline avg drives the aggregates (one filter, one metric)
        assert model['global']['n_referenced'] == 1
        html_str = calibration.render_html(model, 'spread')
        # Columns are ordered avg / min / max on both sides and for the deltas
        for col in ('<th class="grp">Ref avg</th>', '<th>Ref min</th>',
                    '<th>Ref max</th>', '<th class="grp">Meas avg</th>',
                    '<th>Meas min</th>', '<th>Meas max</th>',
                    '<th class="grp">Δ avg</th>', '<th>Δ min</th>',
                    '<th>Δ max</th>'):
            assert col in html_str, col
        assert '-20.0%' in html_str and '+28.6%' in html_str
        # Deltas come first, then the trend, then the raw numbers
        assert html_str.index('<th class="grp">Δ avg</th>') < \
            html_str.index('Impr. vs prev') < \
            html_str.index('<th class="grp">Ref avg</th>') < \
            html_str.index('<th class="grp">Meas avg</th>')
        # The fixed-scale delta bar is gone
        assert 'dbar' not in html_str
        assert '⋯' not in html_str


class TestCalibrationHistory:
    """Following a metric's calibration across runs."""

    def _db(self, tmp_path, values):
        # One run per value, same declared reference of 100.
        runs = [_run([_result('t:a', 'tgt', 'm', v, ref=100, tol=5,
                              src='rtl')],
                     timestamp=f'2026-07-{16 + i}T10:00:00+00:00')
                for i, v in enumerate(values)]
        for i, r in enumerate(runs):
            r['git_commit'] = f'commit{i}'
        return _make_db(tmp_path, runs)

    def test_history_tracks_delta_per_run(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        points = hist['metrics'][('t:a', 'tgt', 'm')]
        assert [round(p['delta_pct']) for p in points] == [0, 4, 30]
        assert [p['severity'] for p in points] == ['ok', 'ok', 'bad']
        assert [p['git_commit'] for p in points] == \
            ['commit0', 'commit1', 'commit2']
        assert len(hist['runs']) == 3

    def test_find_regressions(self, tmp_path):
        # 0% -> 4% -> 30%: the last step is a 26pp regression
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        regs = calibration.find_regressions(hist, threshold_pp=2.0)
        assert len(regs) == 1
        assert round(regs[0]['drift_pp']) == 26
        assert regs[0]['to_commit'] == 'commit2'
        # A model that got *better* is not a regression
        assert calibration.find_regressions(
            {'runs': hist['runs'],
             'metrics': {('t', 'x', 'm'): [
                 {'delta_pct': 30, 'git_commit': 'a', 'run_id': 1},
                 {'delta_pct': 2, 'git_commit': 'b', 'run_id': 2}]}},
            threshold_pp=2.0) == []

    def test_history_uses_baseline_when_given(self, tmp_path):
        # ACU-style: reference comes from an rtl run, reused across history
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'm', 100)], platform='rtl',
                 timestamp='2026-07-16T09:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 110)], platform='gvsoc',
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'm', 150)], platform='gvsoc',
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        conn = sqlite3.connect(db)
        baseline = calibration.query_results(conn, platform='rtl')
        hist = calibration.build_history(
            calibration.query_history(conn, exclude_platform='rtl'),
            baseline=baseline)
        conn.close()
        points = hist['metrics'][('t:a', 'tgt', 'm')]
        assert [round(p['delta_pct']) for p in points] == [10, 50]
        assert calibration.find_regressions(hist)[0]['drift_pp'] == 40

    def test_trend_columns(self, tmp_path):
        # |Δ| goes 20% -> 10% -> 5%: improving each run.
        db = self._db(tmp_path, [120, 110, 105])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist, window_days=30)
        cell = model['cells'][0]
        # vs previous run: |Δ| 10% -> 5% == 50% improvement
        assert round(cell['trend_prev_pct']) == 50
        # vs the 30d average of the earlier runs (mean(20, 10) = 15) -> 5%
        assert round(cell['trend_window_pct']) == 67
        html_str = calibration.render_html(model, 'trend', history=hist)
        assert 'Impr. vs prev' in html_str
        assert 'Impr. vs 30d avg' in html_str

    def test_trend_marks_worsening(self, tmp_path):
        # |Δ| 5% -> 20%: got four times worse
        db = self._db(tmp_path, [105, 120])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        assert round(model['cells'][0]['trend_prev_pct']) == -300

    def test_trends_absent_without_history(self, tmp_path):
        # A single-run database still renders; trend columns show as "—"
        db = self._db(tmp_path, [110])
        conn = sqlite3.connect(db)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        assert model['cells'][0]['trend_prev_pct'] is None
        html_str = calibration.render_html(model, 'no history')
        assert 'Impr. vs prev' in html_str

    def test_history_section_rendered(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        html_str = calibration.render_html(model, 'hist', history=hist)
        assert 'Calibration over time' in html_str
        assert '<svg class="spark"' in html_str
        # A single run has nothing to trend
        assert 'Calibration over time' not in calibration.render_html(
            model, 'hist', history={'runs': hist['runs'][:1], 'metrics': {}})

    def test_delta_history_per_row(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        cell = model['cells'][0]
        # points carry the run id; the report's run axis holds commit/date
        assert cell['delta_history'] == [[0.0, 'ok', 1], [4.0, 'ok', 2],
                                         [30.0, 'bad', 3]]
        assert cell['tol_pct'] == 5.0     # declared tol 5 on a ref of 100

        html_str = calibration.render_html(model, 'hist', history=hist)
        assert '<th class="txt">Δ history · trend</th>' in html_str
        assert ('<script type="application/json" id="calib-runs">'
                '[[1,"commit0","2026-07-16T10:00"],'
                '[2,"commit1","2026-07-17T10:00"],'
                '[3,"commit2","2026-07-18T10:00"]]</script>') in html_str

        m = re.search(r'<td class="dh"><div class="sp" data-h="([^"]*)" '
                      r'data-tol="5">', html_str)
        assert json.loads(html.unescape(m.group(1))) == cell['delta_history']
        # Window buttons, last 10 selected by default
        for n in ('5', '10', '100', '0'):
            assert f'data-n="{n}"' in html_str
        assert 'data-n="10" aria-pressed="true"' in html_str
        assert "'.sp[data-h]'" in html_str      # the drawing script

    def test_metric_added_later_keeps_the_run_axis(self, tmp_path):
        # 'late' is only measured in the last of the three runs: its point
        # carries that run's id, so the report can place it under the other
        # rows' last point instead of centring it.
        runs = [_run([_result('t:a', 'tgt', 'early', 100 + i, ref=100,
                              src='rtl')],
                     timestamp=f'2026-07-{16 + i}T10:00:00+00:00')
                for i in range(3)]
        runs[-1]['results'].append(
            _result('t:a', 'tgt', 'late', 130, ref=100, src='rtl'))
        conn = sqlite3.connect(_make_db(tmp_path, runs))
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        by_metric = {c['metric']: c for c in model['cells']}
        assert [p[2] for p in by_metric['early']['delta_history']] == [1, 2, 3]
        assert [p[2] for p in by_metric['late']['delta_history']] == [3]
        html_str = calibration.render_html(model, 'axis', history=hist)
        assert '"calib-runs">[[1,' in html_str and '[3,' in html_str

    def test_trend_icons(self, tmp_path):
        db = self._db(tmp_path, [100, 104, 130])
        conn = sqlite3.connect(db)
        rows = calibration.query_results(conn)
        hist = calibration.build_history(calibration.query_history(conn))
        conn.close()
        model = calibration.build_model(rows)
        calibration.annotate_trends(model, hist)
        html_str = calibration.render_html(model, 'hist', history=hist)
        # Legend with the three icons, and the icons handed to the script
        legend = re.search(r'<div class="legend tlegend">(.*?)</div>',
                           html_str).group(1)
        for kind, word in (('imp', 'improving'), ('stable', 'stable'),
                           ('worse', 'getting worse')):
            assert f'<svg class="tr {kind}"' in legend and word in legend
        assert '__TREND_ICONS__' not in html_str
        assert 'var ICONS = {"imp": "<svg class=\\"tr imp\\"' in html_str

    def test_delta_history_absent_without_trends(self, tmp_path):
        # History column and window selector only appear once trends are
        # annotated (a single-run report has no series to draw).
        db = self._db(tmp_path, [110])
        conn = sqlite3.connect(db)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        html_str = calibration.render_html(model, 'no history')
        assert 'Δ history</th>' not in html_str
        assert 'class="hwin"' not in html_str
        assert '<div class="legend tlegend">' not in html_str


class TestCalibrationRender:

    def test_render_self_contained(self, db_path, tmp_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        html_str = calibration.render_html(model, 'test report')
        for cell in model['cells']:
            assert cell['metric'] in html_str
        # Self-contained: no external resources (inline script only)
        assert 'http://' not in html_str.replace(
            'http://www.apache.org', '')
        assert 'https://' not in html_str
        assert 'cdn.' not in html_str
        assert '<script src' not in html_str

    def test_render_target_filter(self, db_path):
        conn = sqlite3.connect(db_path)
        model = calibration.build_model(calibration.query_results(conn))
        conn.close()
        html_str = calibration.render_html(model, 'test report')
        # One chip per target plus the all-targets default
        for target in ('tgt1', 'sib1', 'sib2', 'gapless'):
            assert f'<button type="button" data-target="{target}"' \
                in html_str
        assert 'data-target=""' in html_str
        # Each target has its own section, tagged for client-side filtering
        for target in ('tgt1', 'sib1', 'sib2', 'gapless'):
            assert f'data-targets="{target}"' in html_str
        # The embedded payload parses and covers every target
        payload = json.loads(
            html_str.split('id="calib-data">')[1].split('</script>')[0]
            .replace('<\\/', '</'))
        assert set(payload['aggregates']) == \
            {'', 'tgt1', 'sib1', 'sib2', 'gapless'}
        assert payload['tolPct'] == 5.0
        assert all(m[1] is not None for m in payload['metrics'])

    def test_cli(self, db_path, tmp_path, capsys):
        output = tmp_path / 'report.html'
        argv = ['calibration', '--db', db_path, '--output', str(output)]
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 0
        assert output.exists()
        assert 'Per-target scoreboard' in output.read_text()
        # The console summary always prints, with the accuracy score.
        out = capsys.readouterr().out
        assert 'CALIBRATION SUMMARY' in out
        assert 'Accuracy score:' in out

    def test_cli_no_match(self, db_path, tmp_path):
        argv = ['calibration', '--db', db_path,
                '--output', str(tmp_path / 'r.html'),
                '--target', 'nonexistent*']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 1


# ---------------------------------------------------------------------------
# Accuracy score, A/B improvement, and ratchet
# ---------------------------------------------------------------------------

def _two_run_db(tmp_path):
    """Baseline (run 1) then a change (run 2): cyc moves closer to its RTL
    reference, lat drifts a little further."""
    return _make_db(tmp_path, [
        _run([_result('t:a', 'tgt', 'cyc', 120, ref=100, tol=5, src='rtl'),
              _result('t:b', 'tgt', 'lat', 210, ref=200, tol=5, src='rtl')],
             timestamp='2026-07-16T10:00:00+00:00'),
        _run([_result('t:a', 'tgt', 'cyc', 103, ref=100, tol=5, src='rtl'),
              _result('t:b', 'tgt', 'lat', 214, ref=200, tol=5, src='rtl')],
             timestamp='2026-07-17T10:00:00+00:00'),
    ])


class TestAccuracyScore:

    def test_score_covers_all_references(self, tmp_path):
        db = _make_db(tmp_path, [_run([
            _result('t:a', 'tgt', 'm1', 110, ref=100, tol=5, src='rtl'),       # 10%
            _result('t:a', 'tgt', 'm2', 102, ref=100, tol=5, src='analytical'),  # 2%
            _result('t:a', 'tgt', 'm3', 130, ref=100, tol=5, src='measured'),  # 30%
            _result('t:a', 'tgt', 'm4', 7),                                     # no ref
        ])])
        conn = init_db(db)
        model = calibration.build_model(calibration.query_results(conn))
        g = model['global']
        assert g['n_accuracy'] == 3                       # rtl + analytical + measured
        assert g['accuracy'] == pytest.approx(14.0)       # mean(10, 2, 30)
        by_metric = {c['metric']: c['acc_err_pct'] for c in model['cells']}
        assert by_metric['m3'] == pytest.approx(30.0)     # measured now counts
        assert by_metric['m4'] is None                    # only the unreferenced
        conn.close()

    def test_score_survives_baseline_substitution(self, tmp_path):
        # Even in run-vs-run (baseline) mode, the accuracy score keeps using
        # the declared ground-truth reference, not the substituted baseline.
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows, baseline=base,
                                        baseline_label='run:1')
        # cyc |Δ|=3%, lat |Δ|=7% against declared refs -> mean 5%
        assert model['global']['accuracy'] == pytest.approx(5.0)
        conn.close()


class TestImprovement:

    def test_net_and_counts(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)            # latest = run 2
        base = calibration.query_results(conn, run=1)
        imp = calibration.build_improvement(rows, base)
        assert imp['n'] == 2
        assert imp['net_pp'] == pytest.approx(7.5)        # 12.5% -> 5.0%
        assert len(imp['improved']) == 1
        assert len(imp['regressed']) == 1
        assert imp['gains'][0]['metric'] == 'cyc'         # biggest gain first
        assert imp['gains'][0]['improve_pp'] == pytest.approx(17.0)
        conn.close()

    def test_measured_lock_included(self, tmp_path):
        db = _make_db(tmp_path, [
            _run([_result('t', 'tgt', 'm', 120, ref=100, tol=34, src='measured')],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t', 'tgt', 'm', 105, ref=100, tol=34, src='measured')],
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        conn = init_db(db)
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        imp = calibration.build_improvement(rows, base)
        assert imp is not None                            # measured now counts
        assert imp['n'] == 1
        assert imp['gains'][0]['improve_pp'] == pytest.approx(15.0)  # 20% -> 5%
        conn.close()

    def test_section_rendered(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows)
        imp = calibration.build_improvement(rows, base)
        html_str = calibration.render_html(
            model, 't', improvement=imp, improvement_baseline='run 1')
        assert 'Accuracy vs baseline' in html_str
        assert 'net accuracy' in html_str
        assert 'Accuracy</th>' in html_str                # scoreboard column
        assert 'id="t-acc"' in html_str                   # headline tile
        conn.close()

    def test_per_metric_column(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        rows = calibration.query_results(conn)
        base = calibration.query_results(conn, run=1)
        model = calibration.build_model(rows)
        imp = calibration.build_improvement(rows, base)
        calibration.annotate_improvement(model, imp)
        # Each cell carries its own improvement (cyc +17 pp, lat -2 pp) ...
        by = {c['metric']: c['improve_pp'] for c in model['cells']}
        assert by['cyc'] == pytest.approx(17.0)
        assert by['lat'] == pytest.approx(-2.0)
        # ... and the detail table renders a per-metric column for them.
        html_str = calibration.render_html(
            model, 't', improvement=imp, improvement_baseline='run 1')
        assert 'Δ acc vs base' in html_str
        assert '+17.0 pp' in html_str and '-2.0 pp' in html_str

    def test_no_column_without_baseline(self, tmp_path):
        conn = init_db(_two_run_db(tmp_path))
        model = calibration.build_model(calibration.query_results(conn))
        html_str = calibration.render_html(model, 't')
        assert 'Δ acc vs base' not in html_str
        conn.close()


class TestRatchet:

    def test_tighten_with_headroom(self, tmp_path):
        # |Δ|=1 sits well inside tol=20 -> tighten (keeps 2x slack).
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 101, ref=100, tol=20, src='rtl')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        rat = calibration.suggest_ratchet(cells)
        assert len(rat['tighten']) == 1
        assert rat['tighten'][0]['tol'] == 20
        assert rat['tighten'][0]['new_tol'] == 2          # ceil(2 * 1)
        assert rat['rebaseline'] == []
        conn.close()

    def test_no_tighten_near_tolerance(self, tmp_path):
        # |Δ|=4 against tol=5: no spare headroom, nothing suggested.
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 104, ref=100, tol=5, src='rtl')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        assert calibration.suggest_ratchet(cells)['tighten'] == []
        conn.close()

    def test_rebaseline_drifted_measured_lock(self, tmp_path):
        db = _make_db(tmp_path, [_run([
            _result('t', 'tgt', 'm', 142, ref=135, tol=34, src='measured')])])
        conn = init_db(db)
        cells = calibration.build_model(calibration.query_results(conn))['cells']
        rat = calibration.suggest_ratchet(cells)
        assert len(rat['rebaseline']) == 1
        assert rat['rebaseline'][0]['ref'] == 135
        assert rat['rebaseline'][0]['new_ref'] == 142
        assert rat['tighten'] == []                       # not a ground truth
        conn.close()


class TestImprovementCli:

    def test_baseline_and_require_improvement(self, tmp_path, capsys):
        db = _two_run_db(tmp_path)
        out = tmp_path / 'r.html'
        argv = ['calibration', '--db', db, '--output', str(out),
                '--baseline-run', '1', '--require-improvement',
                '--suggest-ratchet']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 0               # net +7.5pp -> pass
        text = capsys.readouterr().out
        assert 'CALIBRATION SUMMARY' in text
        assert 'Accuracy score:' in text
        assert 'Accuracy change:' in text
        assert 'IMPROVED by' in text
        assert 'vs baseline run 1' in text
        assert 'Improvement gate: accuracy improved' in text

    def test_require_improvement_fails_on_regression(self, tmp_path, capsys):
        # Swap the runs: run 2 is worse than run 1 -> gate fails.
        db = _make_db(tmp_path, [
            _run([_result('t:a', 'tgt', 'cyc', 103, ref=100, tol=5, src='rtl')],
                 timestamp='2026-07-16T10:00:00+00:00'),
            _run([_result('t:a', 'tgt', 'cyc', 130, ref=100, tol=5, src='rtl')],
                 timestamp='2026-07-17T10:00:00+00:00'),
        ])
        out = tmp_path / 'r.html'
        argv = ['calibration', '--db', db, '--output', str(out),
                '--baseline-run', '1', '--require-improvement']
        import unittest.mock
        with unittest.mock.patch('sys.argv', argv):
            assert calibration.main() == 1
        assert 'did not improve' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Benchmark report: how the applications perform over the runs
# ---------------------------------------------------------------------------

class TestBenchmarkReport:

    def _db(self, tmp_path):
        """Three runs: cycles falling, bandwidth rising, plus a metric that
        is only a calibration point (no kind)."""
        from gvtest.bench import benchmarks
        runs = []
        for i, (cycles, bw) in enumerate(((1000, 50.0), (990, 50.5),
                                          (900, 55.0))):
            results = [
                {**_result('app:fir', 'gap9', 'fir.cycles', cycles),
                 'kind': 'benchmark', 'better': 'lower'},
                {**_result('app:fir', 'gap9', 'fir.bw', bw),
                 'kind': 'benchmark', 'better': 'higher'},
                _result('el:dma', 'gap9', 'dma.cycles', 140, ref=143,
                        src='rtl')]
            runs.append(_run(results,
                             timestamp=f'2026-07-{16 + i}T10:00:00+00:00'))
        return _make_db(tmp_path, runs)

    def _model(self, tmp_path, **kwargs):
        from gvtest.bench import benchmarks
        conn = sqlite3.connect(self._db(tmp_path))
        model = benchmarks.report(conn, **kwargs)
        conn.close()
        return model

    def test_only_tagged_benchmarks(self, tmp_path):
        model = self._model(tmp_path)
        assert {c['metric'] for c in model['cells']} == {'fir.cycles',
                                                         'fir.bw'}

    def test_gain_follows_the_direction(self, tmp_path):
        model = self._model(tmp_path, baseline_run=1)
        by_metric = {c['metric']: c for c in model['cells']}
        # 990 -> 900 cycles and 50.5 -> 55 bytes/cycle are both improvements
        assert round(by_metric['fir.cycles']['gain_prev_pct'], 1) == 9.1
        assert round(by_metric['fir.bw']['gain_prev_pct'], 1) == 8.9
        # against the first run: 1000 -> 900 and 50 -> 55
        assert round(by_metric['fir.cycles']['gain_base_pct'], 1) == 10.0
        assert round(by_metric['fir.bw']['gain_base_pct'], 1) == 10.0
        g = model['global']
        assert (g['n_faster'], g['n_slower']) == (2, 0)
        assert round(g['gain_prev_pct'], 1) == 9.0     # geomean

    def test_a_slower_run_reads_as_such(self, tmp_path):
        from gvtest.bench import benchmarks
        model = self._model(tmp_path)
        cell = dict(next(c for c in model['cells']
                         if c['metric'] == 'fir.cycles'))
        cell['value'] = 1100                  # slower than the 900 before it
        assert benchmarks._verdict(benchmarks._gain_pct(
            benchmarks._change_pct(1100, 900), 'lower')) == 'bad'
        # a change inside the noise band is neither
        assert benchmarks._verdict(benchmarks._gain_pct(
            benchmarks._change_pct(901, 900), 'lower')) == 'warn'

    def test_level_index_and_page(self, tmp_path):
        from gvtest.bench import benchmarks
        model = self._model(tmp_path, baseline_run=1)
        html_str = benchmarks.render_html(model, 'Benchmarks')
        # the levels carry the geomean, the leaves their value
        assert '<th class="grp">Value</th>' in html_str
        assert 'vs baseline' in html_str and 'baseline: run 1' in html_str
        assert '>900<' in html_str and '>55<' in html_str
        # a level's index: 100 at its first run, chained from there
        run_order = [r['run_id'] for r in model['history']['runs']]
        index = benchmarks._index_series(
            benchmarks._root_node(model['clusters']), run_order)
        # run 2 is 1% faster than run 1 (990/1000, 50.5/50), run 3 9.5% more
        assert [round(p[0], 1) for p in index] == [100.0, 101.0, 110.6]
        assert [p[1] for p in index] == [None, 'warn', 'ok']
        # and it is the number the level rows show
        assert '>110.6<' in html_str
        # the calibration-only metric is not in the page
        assert 'dma.cycles' not in html_str

    def _index(self, tmp_path, runs):
        from gvtest.bench import benchmarks
        conn = sqlite3.connect(_make_db(tmp_path, runs))
        model = benchmarks.report(conn)
        conn.close()
        run_order = [r['run_id'] for r in model['history']['runs']]
        return benchmarks._index_series(
            benchmarks._root_node(model['clusters']), run_order)

    def test_a_level_weighs_its_children_equally(self, tmp_path):
        """A test declaring three metrics does not outvote one declaring a
        single metric: under 'app', 'big' and 'small' weigh the same."""
        def bench(test, metric, value):
            return {**_result(test, 'gap9', metric, value),
                    'kind': 'benchmark', 'better': 'lower'}
        runs = []
        for i, (big, small) in enumerate(((100, 100), (90, 100))):
            runs.append(_run(
                [bench('app:big', f'm{n}', big) for n in range(3)]
                + [bench('app:small', 'm0', small)],
                timestamp=f'2026-07-{16 + i}T10:00:00+00:00'))
        index = self._index(tmp_path, runs)
        # 100 -> 90 is a speed ratio of 1.111, so the level moves by
        # geomean(1.111, 1.0) = 1.054, not by geomean of the four metrics
        # (1.111, 1.111, 1.111, 1.0) = 1.082
        assert [round(p[0], 1) for p in index] == [100.0, 105.4]

    def test_a_new_benchmark_does_not_move_the_index(self, tmp_path):
        """Chaining: a benchmark only weighs in once it has a run to be
        compared against, whatever the scale of its values."""
        def bench(metric, value):
            return {**_result('app:fir', 'gap9', metric, value),
                    'kind': 'benchmark', 'better': 'lower'}
        runs = [_run([bench('a', 100)], timestamp='2026-07-16T10:00:00+00:00'),
                # 'b' appears here, ten times bigger, and 'a' is 10% faster
                _run([bench('a', 90), bench('b', 1000)],
                     timestamp='2026-07-17T10:00:00+00:00'),
                _run([bench('a', 90), bench('b', 500)],
                     timestamp='2026-07-18T10:00:00+00:00')]
        index = self._index(tmp_path, runs)
        # run 2 is 'a' alone (+11.1%); only at run 3 does 'b' count, and then
        # the level is geomean(1.0, 2.0) faster
        assert [round(p[0], 1) for p in index] == [100.0, 111.1, 157.1]
