from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backupledger as ledger


def snapshot(ident="a", time="2026-11-24T12:00:00Z", host="nas", paths=None):
    return {"id": ident * 64, "time": time, "hostname": host,
            "paths": paths or ["/srv/data"], "tags": ["daily"],
            "summary": {"total_bytes_processed": 123456}}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"repositories": [{"id": "nas", "name": "NAS", "repository": "/private/repo"}]}))
        self.repo = ledger.load_config(self.config)[0]
        self.db = ledger.connect(self.root / "db.sqlite3")
        self.clock = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def refresh(self, values):
        with patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 0, json.dumps(values), "")):
            self.assertTrue(ledger.refresh(self.db, self.repo))

    def test_snapshots_offsets_optional_fields_and_empty(self):
        raw = snapshot(time="2026-11-24T06:00:00.123456789-07:00")
        del raw["tags"]
        del raw["summary"]
        parsed = ledger.parse_snapshots(json.dumps([raw]))[0]
        self.assertEqual(parsed["time"], "2026-11-24T13:00:00.123456+00:00")
        self.assertEqual(parsed["tags"], [])
        self.assertIsNone(parsed["bytes"])
        self.assertEqual(ledger.parse_snapshots("null"), [])
        self.assertEqual(ledger.parse_snapshots("[]"), [])

    def test_invalid_json_does_not_erase_successful_inventory(self):
        self.refresh([snapshot()])
        for raw in ('{"wrong": []}', '[{"id":"bad"}]', 'not json'):
            with patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 0, raw, "")):
                self.assertFalse(ledger.refresh(self.db, self.repo))
            data = ledger.inventory(self.db, [self.repo], self.clock)["repositories"][0]
            self.assertEqual(len(data["snapshots"]), 1)
            self.assertIn("previous inventory retained", data["refresh_error"])

    def test_failure_preserves_last_capture_and_hides_raw_stderr(self):
        self.refresh([snapshot()])
        with patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 1, "", "secret-password-in-URL")):
            self.assertFalse(ledger.refresh(self.db, self.repo))
        data = ledger.inventory(self.db, [self.repo], self.clock)
        self.assertEqual(len(data["repositories"][0]["snapshots"]), 1)
        self.assertNotIn("secret-password", json.dumps(data))
        self.assertNotIn("/private/repo", json.dumps(data))

    def test_changed_repository_identity_does_not_inherit_old_evidence(self):
        self.refresh([snapshot()])
        with patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 0, "", "")):
            ledger.check_repo(self.db, self.repo, "full")
        changed = {**self.repo, "identity": "new-location"}
        data = ledger.inventory(self.db, [changed], self.clock)["repositories"][0]
        self.assertEqual(data["snapshots"], [])
        self.assertEqual(data["checks"], [])
        with patch.object(ledger, "run_restic", side_effect=OSError("not found")):
            ledger.refresh(self.db, changed)
        data = ledger.inventory(self.db, [changed], self.clock)["repositories"][0]
        self.assertIsNone(data["captured_at"])

    def test_group_freshness_does_not_hide_stale_machine(self):
        self.refresh([snapshot(), snapshot("b", "2026-11-20T12:00:00Z", "laptop"), snapshot("c", "2026-11-22T12:00:00Z")])
        data = ledger.inventory(self.db, [self.repo], self.clock)["repositories"][0]
        groups = {g["hostname"]: g for g in data["groups"]}
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups["nas"]["status"], "recent")
        self.assertEqual(groups["laptop"]["status"], "stale")
        self.assertEqual(groups["nas"]["id"], "a" * 64)

    def test_future_and_missing_expected_groups_are_not_healthy(self):
        self.refresh([snapshot(time="2026-11-30T12:00:00Z")])
        self.repo["expected"] = [{"hostname": "absent", "paths": ["/home"]}]
        groups = ledger.inventory(self.db, [self.repo], self.clock)["repositories"][0]["groups"]
        self.assertEqual({g["status"] for g in groups}, {"future timestamp", "missing"})

    def test_inventory_age_separate_from_snapshot_age(self):
        with patch.object(ledger, "now", return_value="2026-11-20T00:00:00+00:00"):
            self.refresh([snapshot()])
        data = ledger.inventory(self.db, [self.repo], self.clock)["repositories"][0]
        self.assertEqual(data["observation"], "out of date")
        self.assertEqual(data["groups"][0]["status"], "recent")

    def test_check_scopes_failures_timeouts_and_newer_snapshots(self):
        self.refresh([snapshot()])
        with patch.object(ledger, "now", return_value="2026-11-23T00:00:00+00:00"), patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(ledger.check_repo(self.db, self.repo, "full"))
            self.assertEqual(run.call_args.args[1], ["check", "--read-data"])
        with patch.object(ledger, "run_restic", return_value=subprocess.CompletedProcess([], 1, "", "private error")):
            self.assertFalse(ledger.check_repo(self.db, self.repo, "metadata"))
        with patch.object(ledger, "run_restic", side_effect=subprocess.TimeoutExpired("restic", 1)):
            self.assertFalse(ledger.check_repo(self.db, self.repo, "subset", "1/7"))
        checks = ledger.inventory(self.db, [self.repo], self.clock)["repositories"][0]["checks"]
        self.assertEqual([c["result"] for c in checks], ["timeout", "failed", "passed"])
        self.assertEqual(checks[0]["scope"], "subset 1/7")
        self.assertTrue(checks[-1]["newer_snapshots"])

    def test_subset_validation_does_not_run_command(self):
        with patch.object(ledger, "run_restic") as run:
            for value in ("", "0/7", "8/7", "--read-data", "3/0"):
                with self.assertRaises(ValueError):
                    ledger.check_repo(self.db, self.repo, "subset", value)
            run.assert_not_called()

    def test_command_passes_arguments_without_shell_and_clears_host_filter(self):
        repo = {**self.repo, "repository": "/tmp/space and $(command)", "password_file": "/tmp/pw"}
        with patch.dict(os.environ, {"RESTIC_HOST": "hidden-filter"}), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")) as run:
            ledger.run_restic(repo, ["snapshots", "--json"], 42)
        args, kwargs = run.call_args
        self.assertIn("/tmp/space and $(command)", args[0])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("RESTIC_HOST", kwargs["env"])
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_html_escapes_untrusted_backup_metadata(self):
        self.refresh([snapshot(host='<img src=x onerror="alert(1)">', paths=['</td><script>bad()</script>'])])
        page = ledger.render_html(ledger.inventory(self.db, [self.repo], self.clock))
        self.assertNotIn('<img src=x', page)
        self.assertNotIn('<script>bad()', page)
        self.assertIn('&lt;img', page)

    def test_config_additional_repo_expected_paths_and_validation(self):
        repos = [{"id": "a", "repository": "/a"}, {"id": "b", "repository": "/b", "expected": [{"hostname": "new", "paths": ["/z", "/a"]}]}]
        self.config.write_text(json.dumps({"repositories": repos}))
        loaded = ledger.load_config(self.config)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1]["expected"][0]["paths"], ["/a", "/z"])
        repos[1]["id"] = "a"
        self.config.write_text(json.dumps({"repositories": repos}))
        with self.assertRaises(ValueError):
            ledger.load_config(self.config)
        for invalid in (-1, 0, float("nan"), True):
            with self.assertRaises(ValueError):
                ledger.positive(invalid, "threshold")

    def test_private_files(self):
        path = self.root / "export.html"
        ledger.write_private(path, "hello")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "db.sqlite3").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
