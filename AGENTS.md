# Working on Backup Ledger

- This is a private Linux tool, not a GitHub Pages data collection app.
- Never commit actual snapshot metadata, hostnames, paths, credentials or state.
- Keep Python runtime dependencies in the standard library. Collection invokes restic
  with argument arrays, no shell, closed stdin and a bounded timeout.
- Refresh only lists snapshots. Checks require the explicit `check` command; never
  run backup, forget, prune, unlock, repair or restore automatically.
- Keep inventory recency separate from snapshot recency and check scope/result.
  A check passed is not a restore tested; stale and failed captures retain their labels.
- Adding a repository must require config only. Derive filters and counts from data.
- Run `python3 -m unittest discover -s tests -v`; when restic is installed also run
  `python3 tests/restic_smoke.py`. The smoke test uses only a temporary repository.
- Update README instructions and the marked CoolCode guide excerpt for behavior changes.
