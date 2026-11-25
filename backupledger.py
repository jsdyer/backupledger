#!/usr/bin/env python3
"""Private restic inventory. Python 3.10+, standard library only."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

VERSION = "1.0.0"
UTC = timezone.utc


def now():
    return datetime.now(UTC).isoformat()


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(UTC)


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def strings(value, name, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    if nonempty and not value:
        raise ValueError(f"{name} cannot be empty")
    return sorted(set(value))


def load_config(path):
    raw = json.loads(Path(path).expanduser().read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("repositories"), list) or not raw["repositories"]:
        raise ValueError("Config needs a nonempty repositories array")
    repos, seen = [], set()
    for item in raw["repositories"]:
        if not isinstance(item, dict):
            raise ValueError("Each repository must be an object")
        repo = dict(item)
        ident = repo.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident) or ident in seen:
            raise ValueError("Repository IDs must be unique letters, digits, underscores or hyphens")
        seen.add(ident)
        if not isinstance(repo.get("repository"), str) or not repo["repository"].strip():
            raise ValueError(f"{ident}: repository is required")
        repo.setdefault("name", ident)
        if not isinstance(repo["name"], str):
            raise ValueError("Repository name must be a string")
        for key, default in (("stale_after_hours", 48), ("inventory_after_hours", 26), ("check_after_days", 35)):
            repo[key] = positive(repo.get(key, default), key)
        if "password_file" in repo:
            if not isinstance(repo["password_file"], str) or not repo["password_file"]:
                raise ValueError("password_file must be a filename")
            repo["password_file"] = str(Path(repo["password_file"]).expanduser())
        expected = repo.get("expected", [])
        if not isinstance(expected, list):
            raise ValueError("expected must be an array")
        repo["expected"] = []
        for group in expected:
            if not isinstance(group, dict) or not isinstance(group.get("hostname"), str) or not group["hostname"]:
                raise ValueError("Expected group needs hostname and paths")
            repo["expected"].append({"hostname": group["hostname"], "paths": strings(group.get("paths"), "expected paths", True)})
        # A changed location with the same label must not inherit old evidence.
        repo["identity"] = hashlib.sha256(repo["repository"].encode()).hexdigest()
        repos.append(repo)
    return repos


def connect(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS inventory (
        repo TEXT PRIMARY KEY, identity TEXT NOT NULL, attempted TEXT NOT NULL,
        captured TEXT, error TEXT, snapshots TEXT NOT NULL DEFAULT '[]');
      CREATE TABLE IF NOT EXISTS checks (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL, identity TEXT NOT NULL,
        started TEXT NOT NULL, finished TEXT NOT NULL, scope TEXT NOT NULL,
        exit_code INTEGER, result TEXT NOT NULL);
    """)
    return db


def parse_snapshots(raw):
    data = json.loads(raw)
    # Older restic versions may serialize an empty slice as null.
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ValueError("Expected restic's ungrouped JSON snapshot array")
    snapshots, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Snapshot must be an object")
        ident = item.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", ident) or ident in seen:
            raise ValueError("Snapshot has missing, invalid or duplicate full ID")
        seen.add(ident)
        stamp = parse_time(item.get("time")).isoformat()
        hostname = item.get("hostname")
        if not isinstance(hostname, str) or not hostname:
            raise ValueError("Snapshot hostname is required")
        summary = item.get("summary") or {}
        size = summary.get("total_bytes_processed") if isinstance(summary, dict) else None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            size = None
        snapshots.append({"id": ident, "time": stamp, "hostname": hostname,
                          "paths": strings(item.get("paths"), "snapshot paths", True),
                          "tags": strings(item.get("tags") or [], "snapshot tags"), "bytes": size})
    return sorted(snapshots, key=lambda s: s["time"], reverse=True)


def restic_command(repo, args, binary="restic"):
    command = [binary, "--repo", repo["repository"]]
    if repo.get("password_file"):
        command += ["--password-file", repo["password_file"]]
    return command + args


def run_restic(repo, args, timeout, binary="restic"):
    # No shell; stdin closed so an unattended password prompt cannot hang.
    env = os.environ.copy()
    # An inherited host filter would silently hide machines from the inventory.
    env.pop("RESTIC_HOST", None)
    return subprocess.run(restic_command(repo, args, binary), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          timeout=timeout, env=env, check=False)


def refresh(db, repo, timeout=300, binary="restic"):
    attempted = now()
    try:
        result = run_restic(repo, ["snapshots", "--json"], timeout, binary)
        if result.returncode:
            raise RuntimeError(f"restic snapshots exited {result.returncode}; inspect restic locally")
        snapshots = parse_snapshots(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
        # Raw command/stderr can contain credentials and paths; never persist it.
        error = str(exc) if isinstance(exc, RuntimeError) else (
            "restic timed out" if isinstance(exc, subprocess.TimeoutExpired) else
            "Invalid snapshot JSON; previous inventory retained" if isinstance(exc, ValueError) else
            "Could not execute restic; check installation and permissions")
        with db:
            db.execute("""INSERT INTO inventory(repo,identity,attempted,error) VALUES(?,?,?,?)
              ON CONFLICT(repo) DO UPDATE SET attempted=excluded.attempted,error=excluded.error,
              captured=CASE WHEN identity=excluded.identity THEN captured ELSE NULL END,
              snapshots=CASE WHEN identity=excluded.identity THEN snapshots ELSE '[]' END,
              identity=excluded.identity""", (repo["id"], repo["identity"], attempted, error))
        return False
    with db:
        db.execute("""INSERT INTO inventory(repo,identity,attempted,captured,error,snapshots) VALUES(?,?,?,?,NULL,?)
          ON CONFLICT(repo) DO UPDATE SET identity=excluded.identity,attempted=excluded.attempted,
          captured=excluded.captured,error=NULL,snapshots=excluded.snapshots""",
                   (repo["id"], repo["identity"], attempted, now(), json.dumps(snapshots)))
    return True


def check_repo(db, repo, scope, subset=None, timeout=86400, binary="restic"):
    args = ["check"]
    if scope == "full":
        args.append("--read-data")
    elif scope == "subset":
        if not subset or not re.fullmatch(r"\d+/\d+", subset):
            raise ValueError("Subset must be n/t, such as 1/7")
        n, total = map(int, subset.split("/"))
        if not 1 <= n <= total:
            raise ValueError("Subset must satisfy 1 <= n <= total")
        args.append("--read-data-subset=" + subset)
    elif scope != "metadata":
        raise ValueError("Unknown check scope")
    started, code = now(), None
    try:
        result = run_restic(repo, args, timeout, binary)
        code = result.returncode
        outcome = "passed" if code == 0 else "failed"
    except subprocess.TimeoutExpired:
        outcome = "timeout"
    except OSError:
        outcome = "could not run"
    with db:
        db.execute("INSERT INTO checks(repo,identity,started,finished,scope,exit_code,result) VALUES(?,?,?,?,?,?,?)",
                   (repo["id"], repo["identity"], started, now(), f"subset {subset}" if scope == "subset" else scope, code, outcome))
    return outcome == "passed"


def age_hours(stamp, clock):
    return (clock - parse_time(stamp)).total_seconds() / 3600


def inventory(db, repos, clock=None):
    clock = clock or datetime.now(UTC)
    output = {"version": VERSION, "generated_at": clock.isoformat(), "repositories": []}
    for repo in repos:
        row = db.execute("SELECT * FROM inventory WHERE repo=? AND identity=?", (repo["id"], repo["identity"])).fetchone()
        snapshots = json.loads(row["snapshots"]) if row else []
        groups = {}
        for item in snapshots:
            item["age_hours"] = age_hours(item["time"], clock)
            item["status"] = "future timestamp" if item["age_hours"] < -0.0834 else (
                "stale" if item["age_hours"] > repo["stale_after_hours"] else "recent")
            key = (item["hostname"], tuple(item["paths"]))
            if key not in groups:
                groups[key] = dict(item)
        for expected in repo["expected"]:
            key = (expected["hostname"], tuple(expected["paths"]))
            if key not in groups:
                groups[key] = {**expected, "id": None, "time": None, "age_hours": None, "status": "missing", "tags": [], "bytes": None}
        captured = row["captured"] if row else None
        capture_age = age_hours(captured, clock) if captured else None
        observation = "never refreshed" if not captured else (
            "future timestamp" if capture_age < -0.0834 else
            "out of date" if capture_age > repo["inventory_after_hours"] else "current")
        checks = [dict(c) for c in db.execute("SELECT started,finished,scope,exit_code,result FROM checks WHERE repo=? AND identity=? ORDER BY id DESC LIMIT 50", (repo["id"], repo["identity"]))]
        for check in checks:
            check["age_hours"] = age_hours(check["finished"], clock)
            check["overdue"] = check["age_hours"] > repo["check_after_days"] * 24
            check["newer_snapshots"] = any(parse_time(s["time"]) > parse_time(check["started"]) for s in snapshots)
        output["repositories"].append({"id": repo["id"], "name": repo["name"],
            "stale_after_hours": repo["stale_after_hours"], "check_after_days": repo["check_after_days"],
            "inventory_after_hours": repo["inventory_after_hours"], "captured_at": captured,
            "attempted_at": row["attempted"] if row else None, "observation": observation,
            "refresh_error": row["error"] if row else None, "snapshots": snapshots,
            "groups": list(groups.values()), "checks": checks})
    return output


def render_html(data):
    escape = lambda value: html.escape(str(value), quote=True)
    def age(value):
        if value is None:
            return "—"
        if value < 0:
            return "future"
        return f"{value:.1f} h" if value < 48 else f"{value/24:.1f} d"
    cards, group_rows, snapshot_rows, check_rows = [], [], [], []
    for repo in data["repositories"]:
        latest_check = repo["checks"][0] if repo["checks"] else None
        check_label = f'{latest_check["scope"]}: {latest_check["result"]}' if latest_check else "No check recorded"
        issues = sum(g["status"] != "recent" for g in repo["groups"])
        cards.append(f'<article><h2>{escape(repo["name"])}</h2><p><strong>{len(repo["snapshots"])} snapshots</strong> · {len(repo["groups"])} host/path groups</p><p>{issues} groups need attention · threshold {escape(repo["stale_after_hours"])} h</p><p>Inventory: <strong>{escape(repo["observation"])}</strong></p><p class="small">Last captured: {escape(repo["captured_at"] or "never")}<br>Last attempted: {escape(repo["attempted_at"] or "never")}</p><p class="alert">{escape(repo["refresh_error"] or "")}</p><p>Latest recorded check: <strong>{escape(check_label)}</strong></p></article>')
        for items, rows in ((repo["groups"], group_rows), (repo["snapshots"], snapshot_rows)):
            for item in items:
                byte_label = f'{item["bytes"] / 1024**3:.2f} GiB' if item["bytes"] is not None else "unknown"
                cells = [repo["name"], item["hostname"], ", ".join(item["paths"]), item["time"] or "—", age(item["age_hours"]), item["status"], ", ".join(item["tags"]), byte_label, (item["id"] or "—")[:12]]
                rows.append('<tr data-status="'+escape(item["status"])+'">'+''.join('<td>'+escape(c)+'</td>' for c in cells)+'</tr>')
        for check in repo["checks"]:
            notes = []
            if check["overdue"]:
                notes.append("check older than configured interval")
            if check["newer_snapshots"]:
                notes.append("newer snapshots exist")
            cells = [repo["name"], check["finished"], check["scope"], check["result"], check["exit_code"] if check["exit_code"] is not None else "—", age(check["age_hours"]), "; ".join(notes)]
            check_rows.append('<tr>'+''.join('<td>'+escape(c)+'</td>' for c in cells)+'</tr>')
    def table(headers, rows, ident):
        return f'<div class="scroll"><table id="{ident}"><thead><tr>'+''.join('<th scope="col">'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+(''.join(rows) if rows else '<tr><td colspan="'+str(len(headers))+'">No records yet.</td></tr>')+'</tbody></table></div>'
    headings = ["Repository", "Host", "Paths", "Snapshot time (UTC)", "Age", "Freshness", "Tags", "Logical size", "Snapshot ID"]
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Backup Ledger · private restic inventory</title><style>
    :root{color-scheme:dark;font:16px/1.55 system-ui,sans-serif;background:#0c1424;color:#e7edf9}*{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:2rem}h1{font-size:clamp(2rem,5vw,3.4rem);letter-spacing:-.045em;margin:0}h2{font-size:1.2rem}.eyebrow{color:#72d6c5;letter-spacing:.12em;text-transform:uppercase;font-weight:700}.lead{max-width:75ch;color:#c5d0e5}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin:2rem 0}article{background:#14213a;border:1px solid #2c3b57;border-radius:12px;padding:1.2rem}.small{font-size:.875rem;color:#b4c2da;overflow-wrap:anywhere}.alert{color:#ffc690}.scroll{overflow:auto;border:1px solid #2c3b57;border-radius:8px;margin-top:1rem}table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;padding:.75rem;border-bottom:1px solid #2c3b57;vertical-align:top}th{background:#17273e;white-space:nowrap}td{overflow-wrap:anywhere;min-width:7rem}tr[data-status="stale"],tr[data-status="missing"],tr[data-status="future timestamp"]{background:#352720}label{display:inline-block;margin:.5rem 1rem .5rem 0}input,select{font:inherit;color:inherit;background:#14213a;border:1px solid #697c9e;border-radius:5px;padding:.55rem}input:focus,select:focus{outline:2px solid #72d6c5}details{margin:2rem 0}summary{cursor:pointer;font-weight:700}footer{margin-top:2rem;color:#b4c2da}code{color:#90e3d6}@media(max-width:600px){main{padding:1rem}input{max-width:100%}}
    </style></head><body><main><div class="eyebrow">Private backup inventory</div><h1>Backup Ledger</h1><p class="lead">Which machines have fallen behind? Review the newest snapshot for each host and path set, then check the separate integrity-check history. Snapshot freshness is not proof of recoverability.</p><p class="small">Report generated '''+escape(data["generated_at"])+''' · Refresh this page to recalculate ages. Data changes only when the local collector runs.</p><section class="cards" aria-label="Repository summaries">'''+''.join(cards)+'''</section><h2>Latest snapshot by host and path set</h2><label>Search <input id="search" type="search" placeholder="Host, path, repository or tag"></label><label>Show <select id="status"><option value="all">All groups</option><option value="attention">Needs attention</option><option value="recent">Recent</option></select></label><p id="count" role="status"></p>'''+table(headings,group_rows,"groups")+'''<details><summary>All captured snapshots</summary>'''+table(headings,snapshot_rows,"snapshots")+'''</details><h2>Recorded integrity checks</h2><p class="lead">Metadata checks inspect repository structure. Full checks also read stored data. A subset checks only that portion. Each row records a real command outcome; a successful check is not a restore test and does not cover snapshots created afterward. Only the latest 50 checks per repository appear here; the database retains all results.</p>'''+table(["Repository","Finished (UTC)","Scope","Result","Exit code","Age","Notes"],check_rows,"checks")+'''<footer>Everything on this page is served locally. Hostnames, paths and tags can be sensitive: keep reports private. Logical size is the snapshot's source-data size when provided by restic, not repository disk usage.</footer></main><script>
    const search=document.querySelector('#search'),status=document.querySelector('#status');
    function filter(){let shown=0,total=0;document.querySelectorAll('#groups tbody tr[data-status]').forEach(row=>{total++;const kind=row.dataset.status;const matches=row.textContent.toLowerCase().includes(search.value.toLowerCase())&&(status.value==='all'||(status.value==='attention'?kind!=='recent':kind==='recent'));row.hidden=!matches;if(matches)shown++});document.querySelector('#count').textContent=shown+' of '+total+' groups shown';document.querySelectorAll('#snapshots tbody tr[data-status]').forEach(row=>{row.hidden=!row.textContent.toLowerCase().includes(search.value.toLowerCase())})}search.addEventListener('input',filter);status.addEventListener('change',filter);filter();
    </script></body></html>'''


def write_private(path, content):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".backupledger-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def serve(db, repos, port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.send_error(403, "Local host required")
                return
            if self.path not in {"/", "/inventory.json"}:
                self.send_error(404)
                return
            data = inventory(db, repos)
            is_json = self.path.endswith(".json")
            body = (json.dumps(data, indent=2) if is_json else render_html(data)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8" if is_json else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    print(f"Backup Ledger: http://127.0.0.1:{port} (Ctrl+C to stop)", flush=True)
    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.serve_forever()


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", default="~/.config/backupledger/config.json")
    parser.add_argument("--state", default="~/.local/share/backupledger/ledger.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("refresh", "Capture restic snapshot metadata (no integrity check)"), ("check", "Run and record an explicit restic integrity check")):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--repo", help="Configured repository ID; default: all")
        cmd.add_argument("--timeout", type=int, default=300 if name == "refresh" else 86400)
    checker = commands.choices["check"]
    checker.add_argument("--scope", choices=["metadata", "full", "subset"], default="metadata")
    checker.add_argument("--subset", help="n/t, for example 1/7; requires --scope subset")
    exporter = commands.add_parser("export", help="Write a private report without contacting repositories")
    exporter.add_argument("--format", choices=["json", "html"], default="html")
    exporter.add_argument("--output", required=True)
    server = commands.add_parser("serve", help="Read-only dashboard on 127.0.0.1")
    server.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    try:
        repos = load_config(args.config)
        if getattr(args, "repo", None):
            repos = [r for r in repos if r["id"] == args.repo]
            if not repos:
                raise ValueError("Unknown repository ID")
        if getattr(args, "timeout", 1) <= 0:
            raise ValueError("Timeout must be positive")
        if args.command == "check" and args.subset and args.scope != "subset":
            raise ValueError("--subset requires --scope subset")
        if args.command == "serve" and not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        with connect(args.state) as db:
            if args.command in {"refresh", "check"}:
                failed = False
                for repo in repos:
                    ok = refresh(db, repo, args.timeout) if args.command == "refresh" else check_repo(db, repo, args.scope, args.subset, args.timeout)
                    print(f'{repo["id"]}: {args.command} {"succeeded" if ok else "failed (see local ledger)"}')
                    failed = failed or not ok
                return 1 if failed else 0
            if args.command == "export":
                data = inventory(db, repos)
                write_private(args.output, json.dumps(data, indent=2) + "\n" if args.format == "json" else render_html(data))
                print(f"Saved {args.output}")
            else:
                serve(db, repos, args.port)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Config exceptions describe structure, never credential contents.
        print(f"Backup Ledger: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
