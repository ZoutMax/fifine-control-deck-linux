"""Live wire-format test for the Background-portal autostart request.

Runs _portal_autostart against a fake org.freedesktop.portal.Desktop on a
private session bus that validates option types exactly as strictly as the
real xdg-desktop-portal (audit finding: PyQt6 marshalled 'commandline' as
'av', which the real portal rejects wholesale — every request "denied").
Also exercises the CLI path end-to-end: no pre-existing QCoreApplication, so
a regressed throwaway-app bug would hang (and fail) this test.
"""
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _gi_available() -> bool:
    try:
        import gi  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not shutil.which("dbus-run-session"),
                    reason="dbus-run-session not installed")
@pytest.mark.skipif(not _gi_available(), reason="python3-gi not installed")
def test_request_background_marshals_commandline_as_string_array(tmp_path):
    report = tmp_path / "report.txt"
    client = tmp_path / "client.py"
    client.write_text(f"""
import sys, subprocess, time
sys.path.insert(0, {REPO!r})
portal = subprocess.Popen([sys.executable, {os.path.join(HERE, 'portal_harness', 'fake_portal.py')!r}, {str(report)!r}])
time.sleep(1.5)
from fifine_deck.app import _portal_autostart
ok = _portal_autostart(True)
portal.wait(timeout=10)
sys.exit(0 if ok else 3)
""")
    proc = subprocess.run(
        ["dbus-run-session", "--", sys.executable, str(client)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = dict(line.split("=", 1) for line in
                 report.read_text().strip().splitlines())
    assert lines["wire_type"] == "as", lines
    assert lines["typed_as_lookup"] == "OK"
