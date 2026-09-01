"""Upload benchmark results to a bench server (stdlib only).

The payload is the same envelope Runner builds for --bench-db, plus a
client-minted run_uuid (idempotency key) and an optional Jenkins build
reference.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class UploadError(Exception):
    pass


def post_run(url: str, envelope: dict, run_uuid: str,
             build: dict | None = None, timeout: float = 15) -> dict:
    """POST one run envelope to {url}/api/runs.

    Retries once on connection errors and 5xx. Returns the server's
    response dict, or raises UploadError.
    """
    payload = dict(envelope)
    payload['run_uuid'] = run_uuid
    payload['build'] = build
    req = urllib.request.Request(
        url.rstrip('/') + '/api/runs',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})

    last_error: Exception | None = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(1)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise UploadError(
                    f"Bench upload to {url} rejected: "
                    f"{exc.code} {exc.read().decode(errors='replace')}"
                ) from exc
            last_error = exc
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
    raise UploadError(
        f"Bench upload to {url} failed: {last_error}") from last_error


def parse_build(spec: str) -> dict:
    """Parse a JOB:NUMBER --bench-build spec into a build reference."""
    job, sep, number = spec.rpartition(':')
    if not sep or not job or not number.isdigit():
        raise ValueError(
            f"Invalid --bench-build '{spec}', expected JOB:NUMBER")
    return {'job': job, 'build_number': int(number)}
