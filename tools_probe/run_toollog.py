"""Pin one attempt's slice of the tool log.

The framework appends to <results>/<ticker>/<date>/message_tool.log, so a second
run on the same pair reads the first one's lines too. Reading the whole file
would credit a run with gaps it never hit.

Two offsets bound the slice. The start is the file length before the attempt
begins. The end is pinned when the attempt finishes, so the same bytes can be
read again later for review without re-running anything and without picking up
whatever a later run appended.
"""

from __future__ import annotations

import pathlib


class RunLog:
    """The bytes one attempt added to the tool log."""

    def __init__(self, path, start, end=None):
        self.path = path
        self.start = start
        self.end = end

    def read(self):
        if not self.path or not pathlib.Path(self.path).exists():
            return []
        stop = pathlib.Path(self.path).stat().st_size if self.end is None else self.end
        if stop <= self.start:
            return []
        with open(self.path, "rb") as handle:
            handle.seek(self.start)
            blob = handle.read(stop - self.start)
        return blob.decode("utf-8", "replace").splitlines()

    def close(self):
        """Pin the end offset and record both, so a later review can read the
        same bytes again without guessing where this attempt began.

        The framework log is shared by every run of a ticker and date, so a
        review that reads the whole file counts another run''s gaps as this
        run''s. The offsets are what make the slice reproducible.
        """
        if self.end is None and self.path and pathlib.Path(self.path).exists():
            self.end = pathlib.Path(self.path).stat().st_size
        if self.path:
            sidecar = self.path.with_suffix(".offsets.json")
            sidecar.write_text(
                __import__("json").dumps(
                    {"log": str(self.path), "start": self.start, "end": self.end}, indent=2
                ),
                encoding="utf-8",
            )
        return self.end

    def __repr__(self):
        return f"RunLog({self.path}, {self.start}..{self.end})"


def ticker_and_date(cli_args):
    ticker = date = None
    args = list(cli_args)
    for i, arg in enumerate(args):
        if arg == "--ticker" and i + 1 < len(args):
            ticker = args[i + 1]
        elif arg == "--date" and i + 1 < len(args):
            date = args[i + 1]
    return ticker, date


def pin(cli_args, results_dir=None):
    """The log slice for one attempt, starting at the current file length.

    ``results_dir`` must be the value the run is actually configured with; a
    different results dir means a different log, so a default would be wrong.
    """
    ticker, date = ticker_and_date(cli_args)
    if not ticker or not date:
        return None
    if results_dir is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        results_dir = DEFAULT_CONFIG["results_dir"]
    path = pathlib.Path(results_dir) / ticker / date / "message_tool.log"
    # The folder may not exist yet: an attempt that logs nothing still pins
    # cleanly, and creating it here would leave a stray directory behind.
    start = path.stat().st_size if path.exists() else 0
    return RunLog(path, start)
