#!/usr/bin/env python3
"""End-to-end tests: run the real script as a subprocess / inside a pty."""
import os
import select
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "sysvitals.py")


class DumpModeTest(unittest.TestCase):
    """--dump needs no terminal, so it's safe to run as a plain subprocess."""

    @classmethod
    def setUpClass(cls):
        cls.out = subprocess.run(
            [sys.executable, SCRIPT, "--dump"],
            capture_output=True, text=True, timeout=30,
        ).stdout

    def test_runs_without_traceback(self):
        self.assertNotIn("Traceback", self.out)

    def test_banner_shows_product_name(self):
        self.assertIn("SysVitals", self.out)

    def test_sections_present(self):
        for section in ("Top processes by CPU", "Network:", "Listening ports",
                        "Disks", "Mounts"):
            self.assertIn(section, self.out)

    def test_version_flag(self):
        out = subprocess.run(
            [sys.executable, SCRIPT, "--version"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        self.assertRegex(out.strip(), r"^sysvitals \d+\.\d+\.\d+$")


@unittest.skipUnless(hasattr(os, "fork"), "pty tests need a POSIX fork")
class TuiSmokeTest(unittest.TestCase):
    """Drives the curses TUI through every tab in a real pty."""

    def run_pty(self, keys_schedule, total_timeout=15):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", LINES="40", COLUMNS="140")
            os.execv(sys.executable, [sys.executable, SCRIPT, "-i", "0.3"])
        out = b""
        deadline = time.time() + total_timeout
        for delay, keys in keys_schedule:
            if keys:
                os.write(fd, keys)
            end = time.time() + delay
            while time.time() < end and time.time() < deadline:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        out += os.read(fd, 65536)
                    except OSError:
                        break
        try:
            os.write(fd, b"q")
        except OSError:
            pass
        end = time.time() + 2
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")

    def test_all_tabs_render_and_exit_clean(self):
        code, text = self.run_pty([
            (1.2, b""), (0.8, b"2"), (0.8, b"3"), (1.2, b"4"), (0.5, b"\t"),
        ])
        self.assertNotIn("Traceback", text)
        for marker in ("SysVitals", "Processes", "Network", "Ports", "Disks",
                       "PID", "PROTO", "SMART"):
            self.assertIn(marker, text, f"missing {marker!r} in TUI output")
        self.assertEqual(code, 0)

    def test_filter_and_sort_do_not_crash(self):
        code, text = self.run_pty([
            (1.2, b""), (0.5, b"m"), (0.5, b"/py"), (0.5, b"\n"),
        ])
        self.assertNotIn("Traceback", text)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
