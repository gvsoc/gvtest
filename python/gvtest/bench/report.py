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
Benchmark HTML report generator — reads SQLite, produces static HTML with Plotly.js.

Usage:
    python -m gvtest.bench.report --db bench.sqlite --output report/
    python -m gvtest.bench.report --db bench.sqlite --output report/ --test "pulpos:bench:*"
    python -m gvtest.bench.report --db bench.sqlite --output report/ --since 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def query_trends(
    conn: sqlite3.Connection,
    test: str | None = None,
    target: str | None = None,
    since: str | None = None,
) -> dict:
    """Query benchmark trends grouped by test -> metric -> target.

    Returns::

        {
            "test_name": {
                "metric_name": {
                    "desc": "description",
                    "targets": {
                        "target_name": {
                            "timestamps": [...],
                            "values": [...],
                            "commits": [...]
                        }
                    }
                }
            }
        }
    """
    query = """
        SELECT r.test, r.metric, r.target, r.value, r.description,
               ru.timestamp, ru.git_commit
        FROM results r
        JOIN runs ru ON r.run_id = ru.id
        WHERE 1=1
    """
    params: list[str] = []

    if test is not None:
        query += " AND r.test LIKE ?"
        params.append(test.replace('*', '%'))
    if target is not None:
        query += " AND r.target LIKE ?"
        params.append(target.replace('*', '%'))
    if since is not None:
        query += " AND ru.timestamp >= ?"
        params.append(since)

    query += " ORDER BY ru.timestamp ASC"

    rows = conn.execute(query, params).fetchall()

    trends: dict = {}
    for test_name, metric, tgt, value, desc, timestamp, commit in rows:
        if test_name not in trends:
            trends[test_name] = {}
        if metric not in trends[test_name]:
            trends[test_name][metric] = {
                'desc': desc or metric,
                'targets': {},
            }
        metric_data = trends[test_name][metric]['targets']
        if tgt not in metric_data:
            metric_data[tgt] = {
                'timestamps': [],
                'values': [],
                'commits': [],
            }
        metric_data[tgt]['timestamps'].append(timestamp)
        metric_data[tgt]['values'].append(value)
        metric_data[tgt]['commits'].append(
            (commit or '')[:8]
        )

    return trends


def _split_metric(metric_name: str) -> tuple[str, str]:
    """Split 'group.metric' into (group, metric). If no dot, group is ''."""
    if '.' in metric_name:
        group, _, short = metric_name.partition('.')
        return group, short
    return '', metric_name


def _group_metrics(metrics: dict) -> dict[str, list[tuple[str, dict]]]:
    """Group metrics by their prefix.

    Returns { group_name: [(full_metric_name, metric_data), ...] }
    Preserves sort order within each group.
    """
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for metric_name in sorted(metrics.keys()):
        group, _ = _split_metric(metric_name)
        groups[group].append((metric_name, metrics[metric_name]))
    return dict(sorted(groups.items()))


def _trend_info(values: list[float], timestamps: list[str]) -> dict:
    """Compute trend arrow, % vs last run, and % vs last-week average."""
    result = {'arrow': '', 'vs_last': '', 'vs_week': ''}
    if not values:
        return result

    latest = values[-1]

    # vs last run
    if len(values) >= 2:
        prev = values[-2]
        if prev != 0:
            pct = (latest - prev) / abs(prev) * 100
            result['vs_last'] = _format_pct(pct)
            result['arrow'] = _arrow_html(pct)
        else:
            result['vs_last'] = '-'
            result['arrow'] = ''

    # vs last-week average
    if len(values) >= 2 and timestamps:
        try:
            latest_ts = _parse_ts(timestamps[-1])
            week_ago = latest_ts - timedelta(days=7)
            week_values = []
            for i, ts_str in enumerate(timestamps[:-1]):
                ts = _parse_ts(ts_str)
                if ts >= week_ago:
                    week_values.append(values[i])
            if week_values:
                avg = sum(week_values) / len(week_values)
                if avg != 0:
                    pct = (latest - avg) / abs(avg) * 100
                    result['vs_week'] = _format_pct(pct)
                else:
                    result['vs_week'] = '-'
            else:
                result['vs_week'] = '-'
        except Exception:
            result['vs_week'] = '-'

    return result


def _parse_ts(ts_str: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerant of various formats."""
    ts_str = ts_str.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.now(timezone.utc)


def _arrow_html(pct: float) -> str:
    if abs(pct) < 0.01:
        return '<span class="trend stable">&#x2194;</span>'
    if pct > 0:
        return '<span class="trend up">&#x2191;</span>'
    return '<span class="trend down">&#x2193;</span>'


def _format_pct(pct: float) -> str:
    """Format percentage with color: red for increase, green for decrease."""
    sign = '+' if pct > 0 else ''
    if abs(pct) < 0.01:
        cls = 'stable'
    elif pct > 0:
        cls = 'up'
    else:
        cls = 'down'
    return f'<span class="pct {cls}">{sign}{pct:.2f}%</span>'


def _collect_pcts(trends: dict, test_names: list[str],
                   metric_filter: str | None = None) -> list[float]:
    """Collect all vs-last-run percentages for given tests (and optionally a metric group).

    Returns a list of individual % changes — one per (metric, target) pair.
    """
    pcts = []
    for test_name in test_names:
        if test_name not in trends:
            continue
        for metric_name, metric_data in trends[test_name].items():
            if metric_filter is not None:
                group, _ = _split_metric(metric_name)
                if group != metric_filter:
                    continue
            for tgt, data in metric_data['targets'].items():
                vals = data['values']
                if len(vals) >= 2 and vals[-2] != 0:
                    pcts.append((vals[-1] - vals[-2]) / abs(vals[-2]) * 100)
    return pcts


def _aggregate_trend_badge(pcts: list[float]) -> str:
    """Render a small inline trend badge from a list of % changes."""
    if not pcts:
        return ''
    avg = sum(pcts) / len(pcts)
    arrow = _arrow_html(avg)
    # Use compact format for sidebar
    sign = '+' if avg > 0 else ''
    if abs(avg) < 0.01:
        cls = 'stable'
    elif avg > 0:
        cls = 'up'
    else:
        cls = 'down'
    return f' {arrow}<span class="nav-pct {cls}">{sign}{avg:.1f}%</span>'


def _tests_under_prefix(trends: dict, prefix: str) -> list[str]:
    """Return all test names that start with prefix (or equal it)."""
    if not prefix:
        return list(trends.keys())
    return [t for t in trends if t == prefix or t.startswith(prefix + ':')]


def _build_tree(test_names: list[str]) -> dict:
    """Build a nested dict tree from colon-separated test names.

    Leaf nodes (actual tests) have None as value.
    """
    tree: dict = {}
    for name in test_names:
        parts = name.split(':')
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
    return tree


def render_html(trends: dict, output_dir: str) -> str:
    """Generate a static HTML report with Plotly.js charts.

    Returns path to the generated index.html.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, 'index.html')

    tree = _build_tree(sorted(trends.keys()))

    html = _HTML_TEMPLATE
    html = html.replace('{{DATA}}', json.dumps(trends, indent=2))
    html = html.replace('{{NAV_TREE}}', _build_nav_tree(tree, '', trends))
    html = html.replace('{{TEST_SECTIONS}}', _build_test_sections(trends))

    with open(output_path, 'w') as f:
        f.write(html)

    print(f"Report written to {output_path}")
    return output_path


def _build_nav_tree(tree: dict, prefix: str, trends: dict) -> str:
    """Build an HTML nested <ul> tree for sidebar navigation.

    Every node (branch, leaf, metric group) shows an aggregate trend badge.
    """
    items = []
    for name in sorted(tree.keys()):
        full_path = f'{prefix}:{name}' if prefix else name
        children = tree[name]
        anchor = _anchor(full_path)

        if len(children) == 0:
            # Leaf = actual test — show metric groups as sub-items
            if full_path in trends:
                test_pcts = _collect_pcts(trends, [full_path])
                test_badge = _aggregate_trend_badge(test_pcts)
                groups = _group_metrics(trends[full_path])
                if len(groups) == 1 and '' in groups:
                    items.append(
                        f'<li><a href="#test-{anchor}" class="nav-leaf" '
                        f'onclick="showTest(\'{anchor}\')">{name}</a>'
                        f'{test_badge}</li>'
                    )
                else:
                    sub_items = []
                    for group_name in groups:
                        g_label = group_name if group_name else '(ungrouped)'
                        g_anchor = f'{anchor}--{_anchor(group_name)}' if group_name else anchor
                        g_pcts = _collect_pcts(trends, [full_path], metric_filter=group_name)
                        g_badge = _aggregate_trend_badge(g_pcts)
                        sub_items.append(
                            f'<li><a href="#group-{g_anchor}" class="nav-leaf" '
                            f'onclick="showGroup(\'{g_anchor}\')">{g_label}</a>'
                            f'{g_badge}</li>'
                        )
                    items.append(
                        f'<li><details open><summary><a href="#test-{anchor}" class="nav-test" '
                        f'onclick="showTest(\'{anchor}\')">{name}</a>'
                        f'{test_badge}</summary>'
                        f'<ul>{"".join(sub_items)}</ul></details></li>'
                    )
            else:
                items.append(
                    f'<li><a href="#test-{anchor}" class="nav-leaf" '
                    f'onclick="showTest(\'{anchor}\')">{name}</a></li>'
                )
        else:
            # Branch — aggregate trend from all tests under this prefix
            branch_tests = _tests_under_prefix(trends, full_path)
            branch_pcts = _collect_pcts(trends, branch_tests)
            branch_badge = _aggregate_trend_badge(branch_pcts)
            sub_tree = _build_nav_tree(children, full_path, trends)
            items.append(
                f'<li><details open><summary>{name}{branch_badge}</summary>'
                f'<ul>{sub_tree}</ul></details></li>'
            )

    return '\n'.join(items)


def _build_test_sections(trends: dict) -> str:
    """Build per-test sections with metric groups as sub-sections."""
    sections = []
    for test_name in sorted(trends.keys()):
        metrics = trends[test_name]
        anchor = _anchor(test_name)
        groups = _group_metrics(metrics)

        group_htmls = []
        chart_idx = 0
        for group_name, group_metrics in groups.items():
            g_anchor = f'{anchor}--{_anchor(group_name)}' if group_name else anchor
            g_label = group_name if group_name else test_name.rsplit(':', 1)[-1]

            # Metric summary rows
            metric_rows = []
            for metric_name, metric_data in group_metrics:
                _, short_name = _split_metric(metric_name)
                for tgt in sorted(metric_data['targets'].keys()):
                    data = metric_data['targets'][tgt]
                    latest = data['values'][-1] if data['values'] else None
                    val_str = f"{latest:.2f}" if latest is not None else '-'
                    info = _trend_info(data['values'], data['timestamps'])
                    metric_rows.append(
                        f'<tr class="metric-row" data-target="{tgt}">'
                        f'<td>{metric_data["desc"]}</td>'
                        f'<td><code>{short_name}</code></td>'
                        f'<td>{tgt}</td>'
                        f'<td class="num">{val_str}</td>'
                        f'<td class="num">{info["arrow"]} {info["vs_last"]}</td>'
                        f'<td class="num">{info["vs_week"]}</td>'
                        f'<td class="num">{len(data["values"])}</td>'
                        f'</tr>'
                    )

            # Chart grid for this group
            chart_cells = []
            for metric_name, _ in group_metrics:
                div_id = f'chart-{anchor}-{chart_idx}'
                chart_cells.append(
                    f'<div class="chart-cell"><div id="{div_id}" class="chart"></div></div>'
                )
                chart_idx += 1

            group_htmls.append(
                f'<div class="metric-group" id="group-{g_anchor}">'
                f'<h3>{g_label}</h3>'
                f'<table class="metric-table">'
                f'<thead><tr>'
                f'<th>Description</th><th>Metric</th><th>Target</th>'
                f'<th>Latest</th><th>vs last run</th><th>vs week avg</th><th>Runs</th>'
                f'</tr></thead>'
                f'<tbody>{"".join(metric_rows)}</tbody>'
                f'</table>'
                f'<div class="chart-grid">{"".join(chart_cells)}</div>'
                f'</div>'
            )

        sections.append(
            f'<div class="test-section" id="test-{anchor}">'
            f'<h2>{test_name}</h2>'
            f'{"".join(group_htmls)}'
            f'</div>'
        )
    return '\n'.join(sections)


def _anchor(name: str) -> str:
    """Convert a test name to an HTML-safe anchor."""
    return name.replace(':', '-').replace(' ', '-').replace('.', '-')


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benchmark Report</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5; color: #333;
    display: flex; min-height: 100vh;
  }

  /* --- Sidebar --- */
  .sidebar {
    width: 260px; min-width: 260px;
    background: #fff; border-right: 1px solid #e0e0e0;
    padding: 16px 12px; overflow-y: auto;
    position: sticky; top: 0; height: 100vh;
  }
  .sidebar h2 { font-size: 15px; margin-bottom: 12px; color: #555; }
  .sidebar ul { list-style: none; padding-left: 0; }
  .sidebar li { margin: 0; }
  .sidebar li ul { padding-left: 16px; }
  .sidebar details { border: none; margin: 0; }
  .sidebar summary {
    cursor: pointer; font-size: 13px; font-weight: 600; color: #555;
    padding: 4px 0; user-select: none;
  }
  .sidebar summary:hover { color: #2980b9; }
  .nav-leaf, .nav-test {
    display: inline; font-size: 13px; color: #333;
    text-decoration: none;
  }
  .nav-leaf { display: inline; padding: 3px 0; }
  .sidebar li > .nav-leaf { display: inline; }
  .nav-leaf:hover, .nav-leaf.active,
  .nav-test:hover { color: #2980b9; font-weight: 600; }
  .nav-pct { font-size: 11px; margin-left: 1px; font-weight: 400; }
  .nav-pct.up { color: #e74c3c; }
  .nav-pct.down { color: #2ecc71; }
  .nav-pct.stable { color: #aaa; }

  /* --- Main content --- */
  .main {
    flex: 1; padding: 24px 32px; max-width: 1200px; overflow-x: hidden;
  }
  h1 { margin-bottom: 4px; font-size: 22px; }
  .subtitle { color: #888; margin-bottom: 20px; font-size: 13px; }

  /* --- Filter bar --- */
  .filter-bar { margin-bottom: 20px; }
  .filter-bar label { font-size: 13px; color: #555; }
  .filter-bar select {
    padding: 5px 8px; font-size: 13px; border: 1px solid #ccc; border-radius: 4px;
    margin-left: 4px;
  }

  /* --- Test sections --- */
  .test-section {
    background: #fff; border-radius: 8px; padding: 20px;
    margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
  }
  .test-section h2 {
    font-size: 15px; color: #333; margin-bottom: 16px;
    padding-bottom: 8px; border-bottom: 2px solid #e0e0e0;
  }

  /* --- Metric groups (sub-tests) --- */
  .metric-group {
    margin-bottom: 20px; padding: 12px;
    border: 1px solid #f0f0f0; border-radius: 6px;
    background: #fcfcfc;
  }
  .metric-group:last-child { margin-bottom: 0; }
  .metric-group h3 {
    font-size: 14px; color: #2980b9; margin-bottom: 10px;
    padding-bottom: 6px; border-bottom: 1px solid #eee;
  }

  /* --- Metric summary table --- */
  .metric-table {
    width: 100%; border-collapse: collapse; margin-bottom: 12px;
    font-size: 13px;
  }
  .metric-table th, .metric-table td {
    padding: 5px 8px; text-align: left; border-bottom: 1px solid #f0f0f0;
  }
  .metric-table th {
    background: #fafafa; font-weight: 600; font-size: 11px;
    color: #777; text-transform: uppercase; letter-spacing: 0.3px;
  }
  .metric-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .metric-table code {
    font-size: 12px; background: #f0f0f0; padding: 1px 5px; border-radius: 3px;
  }
  .metric-table tr:hover { background: #f8f8f8; }

  /* --- Chart grid --- */
  .chart-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  }
  .chart-cell { min-width: 0; }
  .chart { width: 100%; height: 280px; }

  /* --- Trend arrows & percentages --- */
  .trend { font-size: 13px; }
  .trend.up { color: #e74c3c; }
  .trend.down { color: #2ecc71; }
  .trend.stable { color: #aaa; }
  .pct { font-size: 12px; margin-left: 2px; }
  .pct.up { color: #e74c3c; }
  .pct.down { color: #2ecc71; }
  .pct.stable { color: #aaa; }

  /* --- Responsive --- */
  @media (max-width: 900px) {
    .sidebar { display: none; }
    .chart-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<nav class="sidebar">
  <h2>Tests</h2>
  <ul>
  {{NAV_TREE}}
  </ul>
</nav>

<div class="main">

<h1>Benchmark Report</h1>
<p class="subtitle">Generated by gvtest</p>

<div class="filter-bar">
  <label>Filter by target:</label>
  <select id="target-filter" onchange="filterTarget(this.value)">
    <option value="">All targets</option>
  </select>
</div>

{{TEST_SECTIONS}}

</div><!-- .main -->

<script>
const DATA = {{DATA}};

// Collect all targets for the filter dropdown
const allTargets = new Set();
for (const test of Object.values(DATA)) {
  for (const metric of Object.values(test)) {
    for (const target of Object.keys(metric.targets)) {
      allTargets.add(target);
    }
  }
}
const sel = document.getElementById('target-filter');
for (const t of [...allTargets].sort()) {
  const opt = document.createElement('option');
  opt.value = t; opt.textContent = t;
  sel.appendChild(opt);
}

// Color palette
const COLORS = [
  '#2980b9', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6',
  '#1abc9c', '#e67e22', '#34495e', '#16a085', '#c0392b'
];

function renderCharts(targetFilter) {
  const testNames = Object.keys(DATA).sort();
  for (let ti = 0; ti < testNames.length; ti++) {
    const testName = testNames[ti];
    const metrics = DATA[testName];
    const metricNames = Object.keys(metrics).sort();
    const anchor = testName.replace(/:/g, '-').replace(/ /g, '-').replace(/\\./g, '-');

    for (let mi = 0; mi < metricNames.length; mi++) {
      const metricName = metricNames[mi];
      const metric = metrics[metricName];
      const divId = 'chart-' + anchor + '-' + mi;
      const div = document.getElementById(divId);
      if (!div) continue;

      const traces = [];
      const targets = Object.keys(metric.targets).sort();
      let ci = 0;
      for (const target of targets) {
        if (targetFilter && target !== targetFilter) continue;
        const d = metric.targets[target];
        traces.push({
          x: d.timestamps,
          y: d.values,
          name: target,
          type: 'scatter',
          mode: 'lines+markers',
          line: { color: COLORS[ci % COLORS.length], width: 2 },
          marker: { size: 4 },
          text: d.commits.map((c, i) => target + '<br>commit: ' + c + '<br>value: ' + d.values[i]),
          hoverinfo: 'text+x',
        });
        ci++;
      }

      // Extract short metric name (after the dot)
      const shortName = metricName.includes('.') ? metricName.split('.').slice(1).join('.') : metricName;

      Plotly.react(divId, traces, {
        title: { text: metric.desc || shortName, font: { size: 12 } },
        xaxis: { title: '', tickfont: { size: 10 } },
        yaxis: { title: shortName, tickfont: { size: 10 } },
        margin: { t: 28, b: 36, l: 50, r: 10 },
        legend: { orientation: 'h', y: -0.28, font: { size: 10 } },
        hovermode: 'closest',
      }, { responsive: true });
    }
  }
}

function filterTarget(value) {
  renderCharts(value);
  document.querySelectorAll('.metric-row').forEach(row => {
    if (!value) { row.style.display = ''; return; }
    row.style.display = (row.dataset.target === value) ? '' : 'none';
  });
}

function showTest(anchor) {
  const el = document.getElementById('test-' + anchor);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function showGroup(anchor) {
  const el = document.getElementById('group-' + anchor);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Initial render
renderCharts('');
</script>

</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate benchmark HTML report'
    )
    parser.add_argument('--db', required=True,
                        help='SQLite database path')
    parser.add_argument('--output', required=True,
                        help='Output directory for HTML report')
    parser.add_argument('--test', default=None,
                        help='Filter by test name (supports * wildcard)')
    parser.add_argument('--target', default=None,
                        help='Filter by target name (supports * wildcard)')
    parser.add_argument('--since', default=None,
                        help='Only include runs since this date (ISO 8601)')

    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    trends = query_trends(conn, test=args.test, target=args.target,
                          since=args.since)
    conn.close()

    if not trends:
        print("No benchmark data found matching the filters.")
        sys.exit(0)

    render_html(trends, args.output)


if __name__ == '__main__':
    main()
