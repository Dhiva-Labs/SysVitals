#!/usr/bin/env python3
"""Smoke test for an installed SysVitals package (/usr/bin/sysvitals).

verify.sh runs it in the Fedora, openSUSE and Arch containers after the
package is installed, and aur.sh after the AUR build: --version, --dump, and
the curses TUI driven through every tab in a pty, the same checks as
tests/test_integration.py but against the installed command and the
distribution's own Python.
"""

import os
import re
import select
import shutil
import subprocess
import sys
import time

CMD = "/usr/bin/sysvitals"


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    # realpath: on Fedora and Arch /usr/sbin is a symlink to /usr/bin and
    # comes first on root's PATH.
    found = shutil.which("sysvitals")
    if not found or os.path.realpath(found) != CMD:
        fail(f"sysvitals on PATH is {found!r}, not {CMD}")

    out = subprocess.run([CMD, "--version"], capture_output=True, text=True, timeout=30)
    print(out.stdout.strip(), out.stderr.strip())
    if out.returncode != 0 or not re.fullmatch(r"sysvitals \d+\.\d+\.\d+", out.stdout.strip()):
        fail("--version")

    out = subprocess.run([CMD, "--dump"], capture_output=True, text=True, timeout=60)
    print(out.stdout[:2000])
    if out.returncode != 0 or "Traceback" in out.stdout + out.stderr:
        fail(f"--dump exited {out.returncode}: {out.stderr}")
    for section in ("SysVitals", "Top processes by CPU", "Network:", "Listening ports",
                    "Disks", "Mounts"):
        if section not in out.stdout:
            fail(f"--dump has no {section!r} section")

    import pty

    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", LINES="40", COLUMNS="140")
        os.execv(CMD, [CMD, "-i", "0.3"])
    text = b""
    for delay, keys in ((1.5, b""), (0.8, b"2"), (0.8, b"3"), (1.5, b"4"), (0.5, b"q")):
        if keys:
            os.write(fd, keys)
        end = time.time() + delay
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    text += os.read(fd, 65536)
                except OSError:
                    break
    end = time.time() + 5
    while time.time() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        fail("the TUI did not quit on 'q'")
    screen = text.decode("utf-8", "replace")
    if "Traceback" in screen:
        print(screen)
        fail("traceback in the TUI")
    for marker in ("SysVitals", "Processes", "Network", "Ports", "Disks", "PID", "PROTO"):
        if marker not in screen:
            fail(f"the TUI never showed {marker!r}")
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        fail(f"the TUI exited with {code}")
    print("sysvitals smoke test passed: --version, --dump, TUI tabs 1-4")


if __name__ == "__main__":
    main()
