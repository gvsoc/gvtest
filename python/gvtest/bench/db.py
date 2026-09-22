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
Benchmark database — ingest JSON results into SQLite.

Usage:
    python -m gvtest.bench.db init   --db bench.sqlite
    python -m gvtest.bench.db insert --json results.json --db bench.sqlite
    python -m gvtest.bench.db list   --db bench.sqlite [--test PATTERN]
    python -m gvtest.bench.db tag    --db bench.sqlite --test 'pulpos:bench:*' \
                                     --kind benchmark [--apply]
    python -m gvtest.bench.db rename --db bench.sqlite --test OLD --to NEW
                                     [--metric-re RE --metric-sub SUB]
                                     [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from gvtest.testsuite import KINDS


_SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    build_number  INTEGER NOT NULL,
    git_commit    TEXT,
    git_branch    TEXT,
    timestamp     TEXT NOT NULL,
    registered_at TEXT,
    UNIQUE(job, build_number)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    git_commit  TEXT,
    git_branch  TEXT,
    platform    TEXT NOT NULL,
    json_file   TEXT,
    build_id    INTEGER REFERENCES builds(id),
    uuid        TEXT,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    test        TEXT NOT NULL,
    target      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    value_min   REAL,
    value_max   REAL,
    description TEXT,
    reference   REAL,
    tolerance   REAL,
    ref_type  TEXT,
    kind        TEXT,
    better      TEXT,
    UNIQUE(run_id, test, target, metric)
);

CREATE INDEX IF NOT EXISTS idx_results_test_metric
    ON results(test, metric);
CREATE INDEX IF NOT EXISTS idx_results_target
    ON results(target);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp
    ON runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_runs_build
    ON runs(build_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_uuid
    ON runs(uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_results_test_target_metric
    ON results(test, target, metric);
"""


_SCHEMA_VERSION = 5


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing database up to the current schema.

    Version 2 added the reference/tolerance/ref_type columns; version 3
    added value_min/value_max (the spread of a metric measured over
    several activations); version 4 added the builds table and the
    build_id/uuid/uploaded_at run columns used by the bench server;
    version 5 added kind/better (what a metric is measured for, and which
    way is an improvement). Older rows read back NULL everywhere.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return
    # Column additions must run before executescript(): the v4 indexes in
    # _SCHEMA reference runs.build_id/uuid, which don't exist yet in an
    # older DB. On a fresh DB the tables don't exist and there is nothing
    # to alter.
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if 'results' in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
        for col, decl in (('reference', 'REAL'), ('tolerance', 'REAL'),
                          ('ref_type', 'TEXT'),
                          ('value_min', 'REAL'), ('value_max', 'REAL'),
                          ('kind', 'TEXT'), ('better', 'TEXT')):
            if col not in cols:
                conn.execute(f"ALTER TABLE results ADD COLUMN {col} {decl}")
    if 'runs' in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        for col, decl in (('build_id', 'INTEGER REFERENCES builds(id)'),
                          ('uuid', 'TEXT'), ('uploaded_at', 'TEXT')):
            if col not in cols:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
    conn.commit()


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if they don't exist. Returns connection."""
    conn = sqlite3.connect(db_path)
    _migrate(conn)
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def insert_json(db_path: str, json_path: str) -> int | None:
    """Insert a JSON results file into the database.

    Returns the run_id of the inserted run, or None if
    the file was already ingested (idempotent).
    """
    json_path = os.path.abspath(json_path)

    with open(json_path, 'r') as f:
        data = json.load(f)

    conn = init_db(db_path)

    # Check if already ingested
    row = conn.execute(
        "SELECT id FROM runs WHERE json_file = ?",
        (json_path,)
    ).fetchone()
    if row is not None:
        print(f"Already ingested: {json_path} (run_id={row[0]})")
        conn.close()
        return None

    cursor = conn.execute(
        "INSERT INTO runs (timestamp, git_commit, git_branch, platform, json_file) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            data.get('timestamp', ''),
            data.get('git_commit'),
            data.get('git_branch'),
            data.get('platform', 'unknown'),
            json_path,
        )
    )
    run_id = cursor.lastrowid

    for result in data.get('results', []):
        conn.execute(
            "INSERT OR IGNORE INTO results "
            "(run_id, test, target, metric, value, value_min, value_max, "
            "description, reference, tolerance, ref_type, kind, better) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                result.get('test', ''),
                result.get('target', ''),
                result.get('metric', ''),
                result.get('value', 0),
                result.get('value_min'),
                result.get('value_max'),
                result.get('description', ''),
                result.get('ref'),
                result.get('tol'),
                result.get('ref_type'),
                result.get('kind'),
                result.get('better', 'lower'),
            )
        )

    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM results WHERE run_id = ?",
        (run_id,)
    ).fetchone()[0]
    print(f"Inserted run_id={run_id}: {count} result(s) from {json_path}")
    conn.close()
    return run_id


def rename(db_path: str, test: str, to_test: str | None = None,
           metric_re: str | None = None, metric_sub: str = '',
           apply: bool = False) -> int:
    """Rename recorded results after a testset was renamed or reorganised.

    Without this the reports see the old and the new name as two metrics and
    the history restarts. `metric_re`/`metric_sub` rewrite the metric name
    too (re.sub), for results that moved to another test with new names.

    Returns the number of rows renamed (0 when nothing matches), or -1 when
    a renamed row would collide with one its run already holds.
    """
    conn = init_db(db_path)
    rows = list(conn.execute(
        'SELECT id, run_id, target, test, metric FROM results WHERE test = ?',
        (test,)))
    pattern = re.compile(metric_re) if metric_re else None
    moves = []
    for rid, run_id, target, old_test, metric in rows:
        new_metric = pattern.sub(metric_sub, metric) if pattern else metric
        new_test = to_test or old_test
        if (new_test, new_metric) != (old_test, metric):
            moves.append((rid, run_id, target, new_test, new_metric,
                          old_test, metric))
    if not moves:
        print(f'Nothing to rename: no results under test {test!r}'
              if not rows else f'Nothing to rename for test {test!r}')
        conn.close()
        return 0

    taken = {(r[0], r[1], r[2], r[3]) for r in conn.execute(
        'SELECT run_id, target, test, metric FROM results')}
    clashes = [m for m in moves if (m[1], m[2], m[3], m[4]) in taken]
    pairs = sorted({(m[5], m[6], m[3], m[4]) for m in moves})
    for old_t, old_m, new_t, new_m in pairs:
        n = sum(1 for m in moves if (m[5], m[6]) == (old_t, old_m))
        print(f'  {old_t}:{old_m} -> {new_t}:{new_m}  ({n} row(s))')
    print(f'{len(moves)} row(s), {len(pairs)} name(s)')
    if clashes:
        print(f'Refusing: {len(clashes)} row(s) would collide with results '
              f'the same run already holds', file=sys.stderr)
        conn.close()
        return -1
    if not apply:
        print('Dry run; pass --apply to write.')
        conn.close()
        return len(moves)
    with conn:
        conn.executemany(
            'UPDATE results SET test = ?, metric = ? WHERE id = ?',
            [(m[3], m[4], m[0]) for m in moves])
    print(f'Renamed {len(moves)} row(s).')
    conn.close()
    return len(moves)


def tag(db_path: str, test: str, kind: str, better: str | None = None,
        metric: str | None = None, apply: bool = False) -> int:
    """Say what already-recorded results are measured for.

    Declarations only reach the results recorded after them, so a test that
    becomes a benchmark leaves its history untagged and out of the
    benchmark report; this puts the tag on the rows already there.

    Returns the number of rows tagged.
    """
    conn = init_db(db_path)
    where = 'test LIKE ?'
    params: list[Any] = [test.replace('*', '%')]
    if metric is not None:
        where += ' AND metric LIKE ?'
        params.append(metric.replace('*', '%'))
    rows = conn.execute(
        f'SELECT COUNT(*), COUNT(DISTINCT test), COUNT(DISTINCT metric) '
        f'FROM results WHERE {where}', params).fetchone()
    print(f'{rows[0]} row(s), {rows[1]} test(s), {rows[2]} metric(s) '
          f'-> kind={kind}' + (f', better={better}' if better else ''))
    if not rows[0]:
        conn.close()
        return 0
    if not apply:
        print('Dry run; pass --apply to write.')
        conn.close()
        return rows[0]
    sets, values = 'kind = ?', [kind]
    if better is not None:
        sets += ', better = ?'
        values.append(better)
    with conn:
        conn.execute(f'UPDATE results SET {sets} WHERE {where}',
                     values + params)
    print(f'Tagged {rows[0]} row(s).')
    conn.close()
    return rows[0]


def list_tests(db_path: str, pattern: str | None = None) -> None:
    """List distinct test/metric combinations in the database."""
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT DISTINCT r.test, r.metric, r.target, r.description,
               COUNT(*) as num_runs,
               MIN(ru.timestamp) as first_run,
               MAX(ru.timestamp) as last_run
        FROM results r
        JOIN runs ru ON r.run_id = ru.id
    """
    params: list[str] = []
    if pattern is not None:
        query += " WHERE r.test LIKE ?"
        params.append(pattern.replace('*', '%'))
    query += " GROUP BY r.test, r.metric, r.target ORDER BY r.test, r.metric, r.target"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("No benchmark results found.")
        conn.close()
        return

    # Simple table output
    fmt = "  {:<50s} {:<35s} {:<20s} {:>5s}  {:<20s}  {:<20s}"
    print(fmt.format("TEST", "METRIC", "TARGET", "RUNS", "FIRST", "LAST"))
    print("  " + "-" * 155)
    for row in rows:
        print(fmt.format(
            row['test'], row['metric'], row['target'],
            str(row['num_runs']), row['first_run'][:19], row['last_run'][:19],
        ))

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Benchmark database tool'
    )
    subparsers = parser.add_subparsers(dest='command')

    p_init = subparsers.add_parser('init', help='Initialize database')
    p_init.add_argument('--db', required=True, help='SQLite database path')

    p_insert = subparsers.add_parser('insert', help='Insert JSON results')
    p_insert.add_argument('--json', required=True, dest='json_file',
                          help='JSON results file')
    p_insert.add_argument('--db', required=True, help='SQLite database path')

    p_rename = subparsers.add_parser(
        'rename', help='Rename recorded results after a testset rename')
    p_rename.add_argument('--db', required=True, help='SQLite database path')
    p_rename.add_argument('--test', required=True,
                          help='Test whose results to rename')
    p_rename.add_argument('--to', dest='to_test', default=None,
                          help='New test name')
    p_rename.add_argument('--metric-re', default=None,
                          help='Regexp matched against the metric name')
    p_rename.add_argument('--metric-sub', default='',
                          help='Replacement for --metric-re (re.sub)')
    p_rename.add_argument('--apply', action='store_true',
                          help='Write the changes (default: dry run)')

    p_tag = subparsers.add_parser(
        'tag', help='Say what already-recorded results are measured for')
    p_tag.add_argument('--db', required=True, help='SQLite database path')
    p_tag.add_argument('--test', required=True,
                       help='Test whose results to tag (supports *)')
    p_tag.add_argument('--metric', default=None,
                       help='Only these metrics (supports *)')
    p_tag.add_argument('--kind', required=True, choices=list(KINDS),
                       help='What the metrics are measured for')
    p_tag.add_argument('--better', default=None, choices=('lower', 'higher'),
                       help='Which way is an improvement')
    p_tag.add_argument('--apply', action='store_true',
                       help='Write the changes (default: dry run)')

    p_list = subparsers.add_parser('list', help='List benchmarks')
    p_list.add_argument('--db', required=True, help='SQLite database path')
    p_list.add_argument('--test', default=None,
                        help='Filter by test name (supports * wildcard)')

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == 'init':
        conn = init_db(args.db)
        print(f"Database initialized: {args.db}")
        conn.close()

    elif args.command == 'insert':
        insert_json(args.db, args.json_file)

    elif args.command == 'rename':
        if args.to_test is None and args.metric_re is None:
            parser.error('rename needs --to and/or --metric-re')
        if rename(args.db, args.test, args.to_test, args.metric_re,
                  args.metric_sub, args.apply) < 0:
            sys.exit(1)

    elif args.command == 'tag':
        tag(args.db, args.test, args.kind, args.better, args.metric,
            args.apply)

    elif args.command == 'list':
        list_tests(args.db, args.test)


if __name__ == '__main__':
    main()
