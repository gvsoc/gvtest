#!/usr/bin/env python3

#
# Copyright (C) 2026 ETH Zurich, University of Bologna and GreenWaves Technologies
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
Benchmark HTML report — how the performance of the applications moves.

The companion of the calibration report: that one asks how close the model
is to its reference, this one asks whether the software and the hardware
are getting faster, over the runs. It takes the metrics a testset declares
as benchmarks (`add_bench(..., kind='benchmark')`, or `set_bench_kind`),
each with the direction that counts as an improvement, and shows the
latest value, its change against the previous run and against a baseline,
and its history.

Usage:
    python -m gvtest.bench.benchmarks --db bench.sqlite --output bench.html
    python -m gvtest.bench.benchmarks --db bench.sqlite --output b.html \\
        --branch main --baseline-branch release
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sqlite3
import sys
from typing import Any

from gvtest.bench import split_description
from gvtest.bench.calibration import (
    _AXIS_JS, _CSS, _DOCS_JS, _TREE_JS, _TREND_ICONS, _VCHART_JS,
    _build_tree, _esc, _indent, _metric_cell, _tree_cells, query_history,
    query_results,
)

# How much a benchmark has to move before it is called faster or slower;
# below this it is noise or a wash. Percent of the value it is compared to.
NOISE_PCT = 2.0


def build_value_history(series: dict[tuple[str, str, str],
                                     list[dict[str, Any]]]) -> dict[str, Any]:
    """The values of every benchmark across the runs, oldest first.

    The calibration history follows the distance to a reference and skips
    what has none; a benchmark usually has none, and it is the value itself
    that is followed here.

    Returns {'runs': [...], 'metrics': {key: [{run_id, value, ...}]}}.
    """
    runs: dict[int, dict[str, Any]] = {}
    metrics: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for key, entries in series.items():
        points = []
        for e in entries:
            if e['value'] is None:
                continue
            points.append({'run_id': e['run_id'], 'timestamp': e['timestamp'],
                           'git_commit': e['git_commit'], 'value': e['value'],
                           'ref': e['ref']})
            runs.setdefault(e['run_id'], {
                'run_id': e['run_id'], 'timestamp': e['timestamp'],
                'git_commit': e['git_commit'], 'platform': e['platform']})
        if points:
            metrics[key] = points
    return {'runs': sorted(runs.values(),
                           key=lambda r: (r['timestamp'], r['run_id'])),
            'metrics': metrics}


def _change_pct(value: float | None, ref: float | None) -> float | None:
    """Change from ref to value, in % of ref."""
    if value is None or not ref:
        return None
    return (value - ref) / abs(ref) * 100


def _gain_pct(change_pct: float | None, better: str) -> float | None:
    """The change, signed so that a positive number is an improvement."""
    if change_pct is None:
        return None
    return -change_pct if better == 'lower' else change_pct


def _verdict(gain_pct: float | None) -> str | None:
    """faster / same / slower, whatever the metric's direction."""
    if gain_pct is None:
        return None
    if gain_pct > NOISE_PCT:
        return 'ok'
    return 'bad' if gain_pct < -NOISE_PCT else 'warn'


def build_model(rows: dict[tuple[str, str, str], dict[str, Any]],
                history: dict[str, Any],
                baseline: dict[tuple[str, str, str], dict[str, Any]] | None
                = None, baseline_label: str = '') -> dict[str, Any]:
    """Assemble the report: one cell per metric, clusters per target.

    Each cell carries the latest value, the change against the previous run
    and against the baseline (as a gain: positive is an improvement), and
    the values over the runs for the sparkline.
    """
    cells = []
    for key, row in rows.items():
        cell = dict(row)
        points = history['metrics'].get(key) or []
        cell['value_history'] = [[p['value'], p['ref'], p['run_id']]
                                 for p in points]
        prev = points[-2]['value'] if len(points) >= 2 else None
        cell['gain_prev_pct'] = _gain_pct(
            _change_pct(cell['value'], prev), cell['better'])
        base = (baseline or {}).get(key)
        cell['gain_base_pct'] = _gain_pct(
            _change_pct(cell['value'], base['value'] if base else None),
            cell['better'])
        cell['baseline'] = base['value'] if base else None
        # The sparkline plots the values, each point coloured like the
        # "vs previous" column: faster, about the same, or slower.
        spark = []
        for i, point in enumerate(points):
            gain = _gain_pct(
                _change_pct(point['value'],
                            points[i - 1]['value'] if i else None),
                cell['better'])
            spark.append([point['value'], _verdict(gain), point['run_id']])
        cell['spark'] = spark
        cells.append(cell)

    clusters = []
    for target in sorted({c['target'] for c in cells}):
        target_cells = [c for c in cells if c['target'] == target]
        rows_out = [{'test': c['test'], 'metric': c['metric'],
                     'desc': c['desc'], 'cells': [c], 'referenced': True}
                    for c in target_cells]
        # Worst regressions first, then the rest by name.
        rows_out.sort(key=lambda r: (
            -(-(r['cells'][0]['gain_prev_pct'] or 0)),
            r['test'], r['metric']))
        clusters.append({
            'targets': [target], 'rows': rows_out,
            'stats': _aggregate(target_cells),
            'n_tests': len({c['test'] for c in target_cells}),
        })

    return {
        'cells': cells,
        'clusters': clusters,
        'targets': [{'target': t['targets'][0], **t['stats']}
                    for t in clusters],
        'global': _aggregate(cells),
        'runs': sorted({(c['run_id'], c['platform'], c['timestamp'],
                         c['git_commit']) for c in cells}),
        'baseline_label': baseline_label,
        'history': history,
    }


def _geomean_gain(gains: list[float]) -> float | None:
    """Geometric mean of the speed ratios behind those gains, back as a %.

    The usual way to summarise a benchmark set: a metric twice as fast and
    one twice as slow cancel out, which the arithmetic mean of percentages
    does not do.
    """
    ratios = [1 + g / 100 for g in gains if g is not None and g > -100]
    if not ratios:
        return None
    return (math.exp(statistics.fmean(math.log(r) for r in ratios)) - 1) * 100


def _aggregate(cells: list[dict[str, Any]]) -> dict[str, Any]:
    prev = [c['gain_prev_pct'] for c in cells
            if c['gain_prev_pct'] is not None]
    base = [c['gain_base_pct'] for c in cells
            if c['gain_base_pct'] is not None]
    worst = min((c for c in cells if c['gain_prev_pct'] is not None),
                key=lambda c: c['gain_prev_pct'], default=None)
    return {
        'n_total': len(cells),
        'n_faster': sum(1 for g in prev if g > NOISE_PCT),
        'n_slower': sum(1 for g in prev if g < -NOISE_PCT),
        'gain_prev_pct': _geomean_gain(prev),
        'gain_base_pct': _geomean_gain(base),
        'worst': worst,
    }


def _index_series(cells: list[dict[str, Any]],
                  run_order: list[int]) -> list[list[Any]]:
    """A level's speed against its own first run, in %, per run.

    Each metric is taken relative to its first recorded value, signed by its
    direction, and the level is the geometric mean of those ratios: +10%
    means the level is a tenth faster than it started.
    """
    first: dict[str, float] = {}
    per_run: dict[int, list[float]] = {}
    for cell in cells:
        for value, _, run_id in cell['spark']:
            key = f"{cell['test']}:{cell['metric']}"
            base = first.setdefault(key, value)
            if not base or value is None:
                continue
            ratio = base / value if cell['better'] == 'lower' else value / base
            per_run.setdefault(run_id, []).append((ratio - 1) * 100)
    points = []
    for run_id in run_order:
        gains = per_run.get(run_id)
        if gains:
            index = _geomean_gain(gains)
            points.append([round(index, 3), _verdict(index), run_id])
    return points


def _pct_cell(gain_pct: float | None, grp: bool = False) -> str:
    """A gain: positive is faster, coloured by _verdict."""
    cls = 'num grp' if grp else 'num'
    if gain_pct is None:
        return f'<td class="{cls}">—</td>'
    sev = _verdict(gain_pct)
    sign = '+' if gain_pct >= 0 else ''
    label = (f'{sign}{gain_pct:.0f}%' if abs(gain_pct) >= 10
             else f'{sign}{gain_pct:.1f}%')
    title = ('faster' if sev == 'ok' else 'slower' if sev == 'bad'
             else f'within ±{NOISE_PCT:g}%')
    return f'<td class="{cls} err {sev}" title="{title}">{_esc(label)}</td>'


def _value(value: float | None) -> str:
    if value is None:
        return '—'
    return f'{value:.0f}' if abs(value) >= 1000 else f'{value:g}'


def _spark(points: list[list[Any]], better: str) -> str:
    data = json.dumps(points, separators=(',', ':'))
    return (f'<div class="sp" data-v="{_esc(data)}" '
            f'data-better="{better}"></div>')


# The sparkline of a benchmark: its values over the runs of the window (the
# report's run axis, so rows line up), and an icon saying whether it is
# getting faster. Direction comes from the metric, not from the numbers: for
# cycles lower is better, for a bandwidth higher is.
_BENCH_JS = r"""
(function () {
  'use strict';
  var cells = document.querySelectorAll('.sp[data-v]');
  if (!cells.length) return;
  var W = 120, H = 26, P = 3;
  var AXIS = window.calibRuns();
  var ICONS = __TREND_ICONS__;
  var NOISE = __NOISE_PCT__;
  var WORDS = {imp: 'faster', stable: 'about the same', worse: 'slower'};

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function fmt(v) {
    return Math.abs(v) >= 1000 ? v.toFixed(0) : String(+v.toPrecision(4));
  }
  function trend(pts, better) {
    if (pts.length < 2) return '';
    var v = pts.map(function (p) { return p[0]; });
    var last = v[v.length - 1];
    var prev = v.slice(0, -1).sort(function (a, b) { return a - b; });
    var h = prev.length >> 1;
    var med = prev.length % 2 ? prev[h] : (prev[h - 1] + prev[h]) / 2;
    if (!med) return '';
    var gain = (better === 'lower' ? (med - last) : (last - med)) / Math.abs(med) * 100;
    var kind = gain > NOISE ? 'imp' : (gain < -NOISE ? 'worse' : 'stable');
    var title = WORDS[kind] + ': ' + fmt(last) + ' against ' + fmt(med) +
        ' (median of the previous ' + prev.length + ' run' +
        (prev.length > 1 ? 's' : '') + '), ' +
        (gain >= 0 ? '+' : '') + gain.toFixed(1) + '%';
    return '<span class="trw" title="' + esc(title) + '">' + ICONS[kind] +
        '</span>';
  }

  function draw(el, n) {
    var all = JSON.parse(el.getAttribute('data-v'));
    var better = el.getAttribute('data-better') || 'lower';
    var win = AXIS.window(n);
    var pts = all.filter(function (p) { return win.has(p[2]); });
    if (!pts.length) { el.textContent = ''; return; }
    var vs = pts.map(function (p) { return p[0]; });
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (hi - lo < 1e-9) { var m = Math.abs(hi) * 0.05 || 1; lo -= m; hi += m; }
    var pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
    function x(p) { return win.x(p[2], P, W - P); }
    function y(v) { return P + (hi - v) / (hi - lo) * (H - 2 * P); }

    var s = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W +
        ' ' + H + '" role="img"><title>' +
        esc(pts.length + ' run(s): ' + fmt(vs[0]) + ' → ' +
            fmt(vs[vs.length - 1])) + '</title>';
    if (pts.length > 1) {
      s += '<polyline class="line" points="' + pts.map(function (p) {
        return x(p).toFixed(1) + ',' + y(p[0]).toFixed(1);
      }).join(' ') + '"/>';
    }
    pts.forEach(function (p, i) {
      var last = i === pts.length - 1;
      s += '<circle class="' + (last ? 'last ' + (p[1] || '') : 'pt') +
          '" cx="' + x(p).toFixed(1) + '" cy="' + y(p[0]).toFixed(1) +
          '" r="' + (last ? 2.8 : 2.2) + '"><title>' +
          esc(fmt(p[0]) + ' · ' + AXIS.label(p[2])) + '</title></circle>';
    });
    el.innerHTML = s + '</svg>' + trend(pts, better);
  }

  function apply(n) {
    Array.prototype.forEach.call(cells, function (el) { draw(el, n); });
  }
  var btns = Array.prototype.slice.call(
      document.querySelectorAll('.hwin button[data-n]'));
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      btns.forEach(function (o) {
        o.setAttribute('aria-pressed',
                       String(o === b));
      });
      apply(+b.getAttribute('data-n'));
      try { localStorage.setItem('bench-hwin', b.getAttribute('data-n')); }
      catch (e) {}
    });
  });
  var n = 10;
  try {
    var saved = localStorage.getItem('bench-hwin');
    if (saved !== null && btns.some(function (b) {
      return b.getAttribute('data-n') === saved;
    })) n = +saved;
  } catch (e) {}
  btns.forEach(function (b) {
    b.setAttribute('aria-pressed', String(+b.getAttribute('data-n') === n));
  });
  apply(n);
})();
"""


def _render_cluster(cluster: dict[str, Any], run_order: list[int],
                    with_baseline: bool) -> str:
    """One target's tree: levels, then their benchmarks."""
    target = cluster['targets'][0]
    stats = cluster['stats']
    meta = (f"{cluster['n_tests']} test(s) · {stats['n_total']} benchmark(s)"
            f" · {stats['n_faster']} faster, {stats['n_slower']} slower "
            f"than the run before")
    out = [f'<section id="sec-{_esc(target)}" '
           f'data-targets="{_esc(target)}"><div class="sec-head">'
           f'<h2>{_esc(target)}</h2>'
           f'<span class="sec-meta">{_esc(meta)}</span></div>'
           f'<div class="scroll"><table><thead><tr>'
           f'<th class="txt">Test / benchmark</th>'
           f'<th class="grp">Value</th>'
           f'<th class="grp">vs previous</th>'
           + ('<th>vs baseline</th>' if with_baseline else '')
           + '<th class="txt grp">History · trend</th>'
           '</tr></thead><tbody>']
    n_rest = 1 if with_baseline else 0
    group_ids = iter(range(1, 1 << 30))

    def group_row(node: dict[str, Any], key: str, gid: int, anc: str,
                  depth: int) -> None:
        cells = _tree_cells(node)
        stats = _aggregate(cells)
        meta = (f"{stats['n_total']} benchmark(s) · {stats['n_faster']} "
                f"faster, {stats['n_slower']} slower")
        worst = stats['worst']
        if worst is not None and stats['n_total'] > 1:
            name = worst['metric']
            if name.startswith(node['prefix']):
                name = name[len(node['prefix']):]
            meta += f' · worst: {name}'
        out.append(f'<tr class="grow" data-gid="{gid}" '
                   f'data-key="{_esc(key)}" data-anc="{anc}">'
                   f'<td class="txt tree"{_indent(depth)}>'
                   f'<button type="button" class="gt" aria-expanded="true">'
                   f'{_esc(node["label"])}</button>'
                   f'<span class="gmeta">{_esc(meta)}</span></td>'
                   f'<td class="num grp">—</td>'
                   f'{_pct_cell(stats["gain_prev_pct"], grp=True)}')
        if with_baseline:
            out.append(_pct_cell(stats['gain_base_pct']))
        # The level's own speed against where it started, as one line
        out.append(f'<td class="dh grp">'
                   f'{_spark(_index_series(cells, run_order), "higher")}</td>'
                   f'</tr>')

    def leaf_row(row: dict[str, Any], anc: str, depth: int) -> None:
        cell = row['cells'][0]
        out.append(f'<tr data-anc="{anc}">'
                   f'{_metric_cell(row["metric"], row["desc"], depth, cell, row["test"])}'
                   f'<td class="num grp" title="{_esc(cell["better"])} is '
                   f'better">{_esc(_value(cell["value"]))}</td>'
                   f'{_pct_cell(cell["gain_prev_pct"], grp=True)}')
        if with_baseline:
            out.append(_pct_cell(cell['gain_base_pct']))
        out.append(f'<td class="dh grp">'
                   f'{_spark(cell["spark"], cell["better"])}</td></tr>')

    def walk(node: dict[str, Any], key: str, anc: str, depth: int) -> None:
        for child in node['children'].values():
            child_key = f"{key}|{child['label']}"
            gid = next(group_ids)
            group_row(child, child_key, gid, anc, depth)
            walk(child, child_key, f'{anc} {gid}'.strip(), depth + 1)
        for row in node['rows']:
            leaf_row(row, anc, depth)

    walk(_build_tree(cluster['rows']), target, '', 0)
    out.append('</tbody></table></div></section>')
    return ''.join(out)


def _fmt_gain(gain: float | None) -> str:
    if gain is None:
        return '—'
    return f"{'+' if gain >= 0 else ''}{gain:.1f}<small>%</small>"


def render_html(model: dict[str, Any], title: str) -> str:
    """The whole page, as a string (self-contained, like the calibration
    report: inline CSS and scripts, no external resources)."""
    g = model['global']
    runs = model['history'].get('runs', [])
    run_order = [r['run_id'] for r in runs]
    runs_axis = [[r['run_id'], (r['git_commit'] or '')[:8],
                  (r['timestamp'] or '')[:16]] for r in runs]
    with_baseline = bool(model['baseline_label'])
    sections = ''.join(_render_cluster(c, run_order, with_baseline)
                       for c in model['clusters'])
    runs_txt = (f"{len(runs)} run(s)"
                + (f" · {runs[0]['git_commit'][:8]} → "
                   f"{runs[-1]['git_commit'][:8]}" if runs else ''))
    if with_baseline:
        runs_txt += f" · baseline: {model['baseline_label']}"
    hist = (
        '<div class="toolbar"><div class="hwin" role="group" '
        'aria-label="History window"><span>History:</span>'
        + ''.join(f'<button type="button" data-n="{n}" '
                  f'aria-pressed="{str(n == 10).lower()}">{label}</button>'
                  for n, label in ((5, 'last 5'), (10, 'last 10'),
                                   (100, 'last 100'), (0, 'all')))
        + '</div><div class="bgroup" role="group" aria-label="Levels">'
          '<button type="button" class="tree-all" data-open="1">Expand all'
          '</button><button type="button" class="tree-all" data-open="0">'
          'Collapse all</button></div>'
          '<button type="button" class="docs-all" aria-pressed="false">'
          'Show all descriptions</button></div>')
    scripts = (_AXIS_JS
               + _BENCH_JS.replace('__TREND_ICONS__',
                                   json.dumps(_TREND_ICONS))
                          .replace('__NOISE_PCT__', repr(NOISE_PCT))
               + _TREE_JS + _VCHART_JS + _DOCS_JS)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style></head><body>
<main>
<p class="eyebrow">gvtest · benchmark report</p>
<h1>{_esc(title)}</h1>
<p class="sub">How the applications perform over the runs. A benchmark is a
metric a testset declares with <code>kind='benchmark'</code>, each with the
direction that counts as an improvement; a change is called faster or slower
beyond ±{NOISE_PCT:g}%. Levels summarise their benchmarks with the geometric
mean of their speed ratios.</p>
<p class="runs">{_esc(runs_txt)}</p>
{hist}
<div class="legend">
  <span><i style="background:var(--ok)"></i>faster</span>
  <span><i style="background:var(--warn)"></i>within ±{NOISE_PCT:g}%</span>
  <span><i style="background:var(--bad)"></i>slower</span>
</div>
<div class="strip">
  <div class="stat"><div class="v">{g['n_total']}</div>
    <div class="k">benchmarks</div></div>
  <div class="stat"><div class="v">{_fmt_gain(g['gain_prev_pct'])}</div>
    <div class="k">against the previous run (geomean)</div></div>
  <div class="stat"><div class="v">{_fmt_gain(g['gain_base_pct'])}</div>
    <div class="k">against the baseline (geomean)</div></div>
  <div class="stat"><div class="v">{g['n_faster']} / {g['n_slower']}</div>
    <div class="k">faster / slower than the run before</div></div>
</div>
{sections}
<footer>Generated by <code>gvtest.bench.benchmarks</code> from the gvtest
bench database. A metric joins this report when its testset declares
<code>add_bench(..., kind='benchmark')</code> (or the test declares
<code>set_bench_kind('benchmark')</code>).
</footer>
</main>
<script type="application/json" id="calib-runs">{json.dumps(runs_axis, separators=(',', ':'))}</script>
<script>{scripts}</script>
</body></html>
"""


def report(conn: sqlite3.Connection, test: str | None = None,
           target: str | None = None, platform: str | None = None,
           branch: str | None = None, job: str | None = None,
           baseline_branch: str | None = None,
           baseline_job: str | None = None,
           baseline_run: int | None = None) -> dict[str, Any]:
    """Query the benchmarks and assemble the model."""
    common = dict(test=test, target=target, platform=platform,
                  kind='benchmark')
    rows = query_results(conn, branch=branch, job=job, **common)
    history = build_value_history(
        query_history(conn, branch=branch, job=job, **common))
    baseline, label = None, ''
    if baseline_branch or baseline_job or baseline_run is not None:
        baseline = query_results(conn, branch=baseline_branch,
                                 job=baseline_job, run=baseline_run, **common)
        label = (f'run {baseline_run}' if baseline_run is not None
                 else f'latest {baseline_branch or baseline_job}')
    return build_model(rows, history, baseline, label)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Benchmark HTML report: application performance over runs')
    parser.add_argument('--db', required=True, help='SQLite database path')
    parser.add_argument('--output', required=True, help='HTML file to write')
    parser.add_argument('--title', default='Benchmarks')
    parser.add_argument('--test', default=None, help='Filter tests (glob)')
    parser.add_argument('--target', default=None, help='Filter targets (glob)')
    parser.add_argument('--platform', default=None)
    parser.add_argument('--branch', default=None,
                        help='Only runs made on this branch')
    parser.add_argument('--job', default=None,
                        help='Only runs produced by this CI job')
    parser.add_argument('--baseline-branch', default=None,
                        help='Measure the selected runs against the latest '
                             'run of that branch')
    parser.add_argument('--baseline-job', default=None)
    parser.add_argument('--baseline-run', type=int, default=None)
    args = parser.parse_args()

    from gvtest.bench.db import init_db
    conn = init_db(args.db)
    model = report(conn, test=args.test, target=args.target,
                   platform=args.platform, branch=args.branch, job=args.job,
                   baseline_branch=args.baseline_branch,
                   baseline_job=args.baseline_job,
                   baseline_run=args.baseline_run)
    conn.close()
    if not model['cells']:
        print('No benchmark matches the given filters. A metric joins this '
              "report when its testset declares kind='benchmark'.",
              file=sys.stderr)
        return 1
    with open(args.output, 'w') as out:
        out.write(render_html(model, args.title))
    g = model['global']
    print(f"{g['n_total']} benchmark(s): {g['n_faster']} faster, "
          f"{g['n_slower']} slower than the run before "
          f"({_fmt_gain(g['gain_prev_pct']).replace('<small>%</small>', '%')} "
          f"geomean) -> {args.output}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
