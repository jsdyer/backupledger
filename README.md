# Backup Ledger

A private Linux dashboard for answering **which backups are old, which machines are missing, and what integrity checks actually ran**. Start with restic; keep the inventory on your own machine.

<!-- coolcode:guide -->
Install locally to compare the newest restic snapshot for each host and path set, flag missing expected backups, and review recorded metadata, full-data or subset integrity checks. A recent snapshot does not prove it can be restored; failed refreshes preserve the previous inventory and display the failure. No snapshot metadata is uploaded to CoolCode.
<!-- /coolcode:guide -->

## What it does

- Lists snapshots from any number of configured restic repositories.
- Shows the newest snapshot **per host and exact path set**, so a healthy NAS cannot hide a stale laptop backup.
- Flags missing expected groups, old snapshots, future timestamps, stale inventory captures and collector failures separately.
- Records the date, scope and actual exit result of an explicitly requested `restic check`. Metadata, full-data and partial-data checks remain distinct.
- Serves a searchable, responsive dashboard on `127.0.0.1` and exports private HTML or JSON reports. All assets are local; there is no telemetry, CDN, account or API key.

This is an **installable tool**, not a public data service. The repository contains only source code and synthetic tests. Python 3.10+ and restic are required; no pip packages are needed. Tested against restic 0.18.1, with CI also testing Ubuntu's packaged restic.

## Install and capture your first inventory

Install Python 3 and restic using your distribution's package manager or the [official restic installation instructions](https://restic.readthedocs.io/en/stable/020_installation.html), then:

```sh
git clone https://github.com/jsdyer/backupledger.git
cd backupledger
install -Dm755 backupledger.py "$HOME/.local/bin/backupledger"
install -Dm600 examples/config.json "$HOME/.config/backupledger/config.json"
```

Edit `~/.config/backupledger/config.json`: replace the example repository location, password-file location and expected host/path group with your own. The password file should already exist and have mode `0600`; the app never creates a repository or asks for its password interactively. Remove the example `expected` entry if you do not yet know the exact host/path values. Then run:

```sh
~/.local/bin/backupledger refresh
~/.local/bin/backupledger serve
```

Open **http://127.0.0.1:8766/**. The server only reads the local inventory; opening or refreshing the page never runs restic. Run `refresh` again after a backup, or enable the local timer below. Configuration changes require restarting `serve`.

Run as the same Linux account that can access your restic repositories and backend credentials. Existing restic authentication environment variables are inherited; `password_file` is optional when you already provide `RESTIC_PASSWORD_FILE` or another supported restic password source. An inherited `RESTIC_HOST` is deliberately removed so it cannot hide other machines. Do not embed passwords in repository URLs or commit your configuration.

## Configure more repositories and expected backups

Every repository has a unique `id` and a human-friendly `name`. Add another object to the `repositories` array; no code changes are needed.

| Field | Meaning | Default |
| --- | --- | --- |
| `id` | Stable local identifier, letters/digits/`_`/`-` | Required |
| `repository` | A restic repository location, passed as one argument | Required |
| `name` | Dashboard label | Same as `id` |
| `password_file` | Local restic password file; `~` expands | Inherited restic authentication |
| `stale_after_hours` | Maximum snapshot age for each host/path group | 48 |
| `inventory_after_hours` | Maximum age of the last successful inventory capture | 26 |
| `check_after_days` | Age after which a recorded check gets an overdue note | 35 |
| `expected` | Array of `{ "hostname": "nas", "paths": ["/srv/data"] }` groups | Empty |

Hostnames and path sets are exact matches, not glob patterns. Path order is ignored. Tags are searchable metadata, not separate freshness groups. Configure `expected` for each backup job you depend on: an entirely absent machine cannot be inferred from snapshots alone. All observed groups remain visible, including retired machines; the app does not change retention in restic.

Changing a repository's location under an existing `id` starts a separate evidence identity: old snapshots/checks will not be presented as evidence for the new location. Removing a config entry hides it without deleting historical check records. If you rename or relocate the same underlying restic repository, use a new ID or deliberately start a new capture history.

## Run and record integrity checks

Inventory refreshes run `restic snapshots --json` only. Checks must be started explicitly:

```sh
# Repository structure and metadata. Does not read all stored file data.
~/.local/bin/backupledger check --repo nas --scope metadata

# Reads all stored data too. Can take a long time and incur remote transfer charges.
~/.local/bin/backupledger check --repo nas --scope full

# Reads one of seven data subsets, accurately labelled as a partial check.
~/.local/bin/backupledger check --repo nas --scope subset --subset 1/7
```

Without `--repo`, each configured repository is checked sequentially. Refresh timeout defaults to 300 seconds per repository; check timeout defaults to 86,400 seconds. Override either with `--timeout SECONDS`.

Results come from the command's actual exit status. Failed commands and timeouts remain visible. The app does **not** infer success from the presence of snapshots, import unverified user assertions, sum subsets into a claimed full check, or call a successful check a restore test. Newer snapshots are explicitly noted when they were created after the check began. The dashboard shows up to 50 latest checks per repository; SQLite retains the complete history.

These commands never invoke `backup`, `forget`, `prune`, `repair`, `unlock` or `restore`. Restic may create its normal transient locks and local cache files when listing/checking a repository; this is not a forensic no-write mode. Credentials still need the permissions required by restic. For command errors, run the corresponding restic command yourself locally to see its diagnostic output. Raw restic stdout/stderr from checks is not stored because it can expose paths or credentials.

## Schedule local inventory refreshes

The supplied **user-level systemd timer** refreshes hourly, with up to five minutes of randomized delay. It does not run integrity checks:

```sh
install -Dm644 systemd/backupledger-refresh.service "$HOME/.config/systemd/user/backupledger-refresh.service"
install -Dm644 systemd/backupledger-refresh.timer "$HOME/.config/systemd/user/backupledger-refresh.timer"
systemctl --user daemon-reload
systemctl --user enable --now backupledger-refresh.timer
systemctl --user list-timers backupledger-refresh.timer
journalctl --user -u backupledger-refresh.service
```

On a server, user timers normally require an active user manager. To keep them running after logout, enable lingering for your account according to your system's policy (`loginctl enable-linger USERNAME`, which may require administrator privileges). Confirm the timer has actually fired. Alternatively call `refresh` from your existing backup job after it finishes.

The optional `~/.config/backupledger/environment` file can hold backend authentication environment variables for the service. Protect it with mode `0600`; systemd environment-file syntax is not a shell script. The service's `UMask=0077` keeps newly created local data private. Set remote backend credentials up locally; **GitHub Actions secrets or CoolCode variables are not used**.

## Reports and remote viewing

```sh
~/.local/bin/backupledger export --format html --output ~/backup-report.html
~/.local/bin/backupledger export --format json --output ~/backup-report.json
```

Reports are created with mode `0600`. Exporting does not contact the repositories. HTML is a frozen report: ages correspond to its generated timestamp. The live localhost dashboard recalculates ages on page load. `/inventory.json` provides the same current local view for your own scripts.

Snapshot metadata includes hostnames, paths and tags. Treat exported reports and the state database as private. Repository URLs/passwords are excluded from exports. The default database is `~/.local/share/backupledger/ledger.sqlite3`; override it with `--state PATH` **before** the subcommand. Likewise use `--config PATH` before a subcommand for another configuration.

To view a server's dashboard from your PC, run `serve` on that server and tunnel to it:

```sh
ssh -L 8766:127.0.0.1:8766 your-server
```

Then open the same localhost URL on your PC. The app intentionally has no public bind option or built-in multi-user authentication. It rejects requests with an unexpected Host header. Do not publish its reports with GitHub Pages or put the dashboard on a public unauthenticated reverse proxy.

## Interpretation and limitations

- **Recent** means a snapshot falls within your configured age threshold. It does not prove a backup job included all intended files or that a restore works.
- **Inventory current** means the listing was captured recently. A recent snapshot in a failed or old capture still needs a successful fresh listing.
- **Logical size** is `summary.total_bytes_processed` when restic provides it. It is not repository disk use, transfer size or deduplicated storage; older snapshots can show `unknown`.
- A check's scope and date matter. Metadata checks do not read every data blob. Test restores separately using restic and your own recovery procedure.
- Only restic is supported initially. There is no automatic discovery of backups, deletions, alerts, backup execution or retention management.

Upstream references: [restic scripting/JSON format](https://restic.readthedocs.io/en/stable/075_scripting.html), [repository checks and subsets](https://restic.readthedocs.io/en/stable/045_working_with_repos.html).

## Development and updates

```sh
python3 -m unittest discover -s tests -v
python3 tests/restic_smoke.py  # needs restic; creates and removes a temporary test repo only
```

Tests cover stale hosts, expected missing backups, future timestamps, failed-capture retention, changed repository identity, timezone parsing, check results/scopes, HTML escaping, local file modes, and additional configured repositories. The integration test runs an actual temporary backup, metadata/full checks, report export and localhost HTTP checks.

Update with `git pull --ff-only` and repeat the `install -Dm755` command; then restart the dashboard. Configuration and SQLite state remain outside the source checkout. This repository's GitHub workflow tests code only. Your private inventory and refresh schedule run on your Linux machine.

Part of [CoolCode](https://coolcode.ca/). MIT licensed.
