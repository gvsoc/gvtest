"""Benchmark database, reports and bench-server upload for gvtest."""


def split_description(desc: str | None) -> tuple[str, str]:
    """Split a benchmark description into (summary, details).

    Descriptions are written as a one-sentence summary followed by more
    sentences explaining the measurement; reports show the summary inline and
    the details on demand. A description with a single sentence has no
    details.
    """
    text = (desc or '').strip()
    head, sep, tail = text.partition('. ')
    if not sep:
        return text, ''
    return head + '.', tail.strip()
