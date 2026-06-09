# Contributing

Thanks for your interest in photohaul. A few things worth knowing before you dig in.

## Running the tests

```sh
make test          # or: python3 -m unittest discover -s tests
```

The suite is stdlib `unittest` with **zero dependencies** and no committed
binaries — the Exif reader/patcher is exercised against minimal in-memory fixtures
(TIFF/JPEG/RAF/CR3) and the copy flow against synthetic cards in a tempdir. Add a
case alongside any behavior change.

The tool's in-camera lock detection reads the BSD `UF_IMMUTABLE` flag, so it (and
parts of the suite) are **macOS-specific**; CI runs on macOS for that reason.

## House style

photohaul is deliberately a single file with **no third-party dependencies** — a
hand-rolled Exif reader and XMP writer, no exiftool, no pip installs. Please keep
it that way:

- Terse, small pure functions; header-only Exif seeks.
- No new third-party packages.
- Keep comments and docstrings current with the code — a stale comment is a bug.
- When you add or change a flag, update all of: `--help` (the argparse epilog),
  `README.md`, and the relevant plan doc.

## Design notes

Non-trivial features start as a dated plan in [`docs/`](docs/)
(`YYYYMMDD_<name>_plan.md`) with a `Status:` line. The directory reads as a
chronological record of how the design evolved. See [`AGENTS.md`](AGENTS.md) for the
fuller map of the source and the invariants that must not break (the card is
read-only, re-runs are idempotent, sidecars are written only for files we placed).
