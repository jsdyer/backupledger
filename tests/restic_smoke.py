"""Integration test using a temporary real restic repository, never user backups."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backupledger as ledger


def main():
    restic = shutil.which("restic")
    if not restic:
        raise SystemExit("restic must be installed to run the integration test")
    with tempfile.TemporaryDirectory(prefix="backupledger-smoke-") as directory:
        root = Path(directory)
        source, repository = root / "source", root / "repo"
        source.mkdir()
        (source / "example.txt").write_text("Temporary fixture, not a real backup.\n")
        password = root / "password"
        password.write_text("temporary-integration-test-password")
        password.chmod(0o600)
        base = [restic, "--repo", str(repository), "--password-file", str(password)]
        def run(args):
            return subprocess.run(args, capture_output=True, text=True, check=True, timeout=120)
        run(base + ["init"])
        run(base + ["backup", "--host", "fixture-host", "--tag", "daily", str(source)])
        config = root / "config.json"
        config.write_text(json.dumps({"repositories": [{"id": "fixture", "repository": str(repository), "password_file": str(password)}]}))
        state = root / "ledger.sqlite3"
        command = [sys.executable, str(ROOT / "backupledger.py"), "--config", str(config), "--state", str(state)]
        run(command + ["refresh"])
        run(command + ["check", "--scope", "metadata"])
        run(command + ["check", "--scope", "full"])
        report = root / "report.json"
        run(command + ["export", "--format", "json", "--output", str(report)])
        data = json.loads(report.read_text())["repositories"][0]
        assert len(data["snapshots"]) == 1, data
        assert data["snapshots"][0]["hostname"] == "fixture-host"
        assert [c["scope"] for c in data["checks"]] == ["full", "metadata"]
        assert all(c["result"] == "passed" for c in data["checks"])
        assert data["observation"] == "current"
        assert str(repository) not in report.read_text()
        assert "temporary-integration-test-password" not in report.read_text()
        # Verify dashboard response and host restriction with a real HTTP request.
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(command + ["serve", "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(50):
                try:
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        page = response.read().decode()
                        assert "fixture-host" in page
                        assert response.headers["Cache-Control"] == "no-store"
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Local dashboard did not start")
            try:
                urlopen(Request(f"http://127.0.0.1:{port}/inventory.json", headers={"Host": "untrusted.example"}), timeout=2)
            except HTTPError as error:
                assert error.code == 403
            else:
                raise AssertionError("Untrusted Host was accepted")
        finally:
            process.terminate()
            process.communicate(timeout=5)
        # A lost repository is visible; inventory is not silently replaced by [] .
        shutil.rmtree(repository)
        failed = subprocess.run(command + ["refresh"], capture_output=True, timeout=20)
        assert failed.returncode == 1
        run(command + ["export", "--format", "json", "--output", str(report)])
        retained = json.loads(report.read_text())["repositories"][0]
        assert len(retained["snapshots"]) == 1
        assert retained["refresh_error"]
        print("Real restic smoke test passed: init, backup, refresh, metadata/full check, export, HTTP privacy, failed refresh retention.")


if __name__ == "__main__":
    main()
