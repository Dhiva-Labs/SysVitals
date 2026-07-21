#!/usr/bin/env python3
"""SysVitals — one terminal tool for processes (htop), connections (netstat),
open ports, and SSD/disk health. Zero dependencies: Python 3.8+ stdlib only.

Tabs:  1 Processes   2 Network   3 Ports   4 Disks
Run with sudo for full NVMe SMART data (works fine without, with less detail).
"""

import argparse
import ctypes
import curses
import fcntl
import glob
import os
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

VERSION = "1.0.0"
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# ---------------------------------------------------------------- helpers

def read_file(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return default


def human_bytes(n, suffix="B"):
    n = float(n)
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}{suffix}" if unit else f"{int(n)}{suffix}"
        n /= 1024.0
    return f"{n:.1f}E{suffix}"


def human_time(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}:{s:02d}"


_UID_CACHE = {}


def uid_name(uid):
    name = _UID_CACHE.get(uid)
    if name is None:
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        _UID_CACHE[uid] = name
    return name


# ---------------------------------------------------------------- CPU / memory

class CpuSampler:
    """Per-core and total CPU%, from /proc/stat deltas (htop's method)."""

    def __init__(self):
        self.prev = {}
        self.percents = {}   # 'cpu' -> total, 'cpu0'.. -> per core
        self.period = 0.0    # per-core total-tick delta of the last window
        self.sample()

    def sample(self):
        ncores = 0
        for line in read_file("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            name = parts[0]
            vals = [int(x) for x in parts[1:]]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            ptotal, pidle = self.prev.get(name, (total, idle))
            dt, di = total - ptotal, idle - pidle
            self.percents[name] = 100.0 * (dt - di) / dt if dt > 0 else 0.0
            self.prev[name] = (total, idle)
            if name == "cpu":
                agg_dt = dt
            else:
                ncores += 1
        # per-core share of the aggregate window; denominator for process CPU%
        self.period = agg_dt / ncores if ncores else 0.0

    @property
    def cores(self):
        return sorted(
            (k for k in self.percents if k != "cpu"),
            key=lambda s: int(s[3:]),
        )


def mem_info():
    info = {}
    for line in read_file("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 1)
    # htop's formula: used = total - free - buffers - (cached + reclaimable - shmem)
    cached = (info.get("Cached", 0) + info.get("SReclaimable", 0)
              - info.get("Shmem", 0))
    used = total - info.get("MemFree", 0) - info.get("Buffers", 0) - cached
    return {
        "total": total,
        "used": max(used, 0),
        "cached": cached,
        "swap_total": info.get("SwapTotal", 0),
        "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
    }


_CPU_TEMP_PATH = None


def cpu_temp():
    global _CPU_TEMP_PATH
    if _CPU_TEMP_PATH is None:
        _CPU_TEMP_PATH = ""
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            name = read_file(hw + "/name").strip()
            if name in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
                _CPU_TEMP_PATH = hw + "/temp1_input"
                break
    raw = read_file(_CPU_TEMP_PATH).strip() if _CPU_TEMP_PATH else ""
    return int(raw) / 1000.0 if raw else None


# ---------------------------------------------------------------- processes

class ProcessSampler:
    def __init__(self):
        self.prev_ticks = {}
        self.procs = []
        self.sample(0.0)

    def sample(self, period):
        """period: per-core total-tick delta from CpuSampler, htop's
        denominator — both sides of the division come from kernel ticks."""
        mem_total = mem_info()["total"]
        new_ticks = {}
        procs = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            stat = read_file(f"/proc/{pid}/stat")
            if not stat:
                continue
            try:
                lpar = stat.index("(")
                comm, _, rest = stat[lpar + 1:].rpartition(")")
                fields = rest.split()
                state = fields[0]
                ppid = int(fields[1])
                utime, stime = int(fields[11]), int(fields[12])
                nthreads = int(fields[17])
                rss = int(fields[21]) * PAGE_SIZE
                uid = os.stat(f"/proc/{pid}").st_uid
            except (ValueError, IndexError, OSError):
                continue
            ticks = utime + stime
            new_ticks[pid] = ticks
            dticks = ticks - self.prev_ticks.get(pid, ticks)
            cpu = 100.0 * dticks / period if period > 0 else 0.0
            kthread = pid == 2 or ppid == 2
            if kthread:
                cmdline = ""
            else:
                cmdline = read_file(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
            procs.append({
                "pid": pid,
                "user": uid_name(uid),
                "state": state,
                "cpu": cpu,
                "mem": 100.0 * rss / mem_total,
                "rss": rss,
                "threads": nthreads,
                "time": ticks / CLK_TCK,
                "name": comm,
                "cmd": cmdline or f"[{comm}]",
                "kthread": kthread,
            })
        self.prev_ticks = new_ticks
        self.procs = procs
        self.n_kthreads = sum(p["kthread"] for p in procs)
        self.n_running = sum(p["state"] == "R" for p in procs)
        # htop's "thr": extra userland threads, main threads not counted
        nuser = len(procs) - self.n_kthreads
        self.n_threads = max(
            sum(p["threads"] for p in procs if not p["kthread"]) - nuser, 0)


# ---------------------------------------------------------------- network

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV",
    "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT",
    "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK",
    "0A": "LISTEN", "0B": "CLOSING",
}


def _decode_addr(hexstr, family):
    addr_hex, _, port_hex = hexstr.partition(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(addr_hex)
    if family == socket.AF_INET:
        ip = socket.inet_ntop(family, raw[::-1])
    else:
        # /proc stores IPv6 as four little-endian 32-bit words
        packed = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        ip = socket.inet_ntop(family, packed)
        if ip.startswith("::ffff:"):
            ip = ip[7:]
    return ip, port


def socket_inode_map():
    """inode -> (pid, comm). Needs same-user or root for each /proc/pid/fd."""
    table = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        comm = read_file(f"/proc/{entry}/comm").strip()
        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                table[int(link[8:-1])] = (int(entry), comm)
    return table


def net_connections():
    """All sockets from /proc/net/{tcp,tcp6,udp,udp6} with owning process."""
    inodes = socket_inode_map()
    conns = []
    sources = [
        ("tcp", "/proc/net/tcp", socket.AF_INET),
        ("tcp6", "/proc/net/tcp6", socket.AF_INET6),
        ("udp", "/proc/net/udp", socket.AF_INET),
        ("udp6", "/proc/net/udp6", socket.AF_INET6),
    ]
    for proto, path, family in sources:
        for line in read_file(path).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                local = _decode_addr(parts[1], family)
                remote = _decode_addr(parts[2], family)
                state = TCP_STATES.get(parts[3], parts[3])
                uid = int(parts[7])
                inode = int(parts[9])
            except (ValueError, OSError):
                continue
            if proto.startswith("udp"):
                state = "LISTEN" if remote[1] == 0 else state
            pid, pname = inodes.get(inode, (None, ""))
            conns.append({
                "proto": proto,
                "laddr": local,
                "raddr": remote,
                "state": state,
                "user": uid_name(uid),
                "pid": pid,
                "pname": pname,
            })
    return conns


def listening_ports(conns=None):
    if conns is None:
        conns = net_connections()
    seen, ports = set(), []
    for c in conns:
        if c["state"] != "LISTEN":
            continue
        key = (c["proto"].rstrip("6"), c["laddr"][1], c["pid"])
        if key in seen:
            continue
        seen.add(key)
        ports.append(c)
    ports.sort(key=lambda c: (c["laddr"][1], c["proto"]))
    return ports


def port_service(port, proto):
    try:
        return socket.getservbyport(port, proto.rstrip("6"))
    except OSError:
        return ""


# ---------------------------------------------------------------- disks / SMART

NVME_IOCTL_ADMIN_CMD = 0xC0484E41
NVME_ADMIN_GET_LOG_PAGE = 0x02
NVME_LOG_SMART = 0x02


CRITICAL_WARNING_FLAGS = [
    (0x01, "spare below threshold"),
    (0x02, "temperature out of range"),
    (0x04, "NVM reliability degraded"),
    (0x08, "media in read-only mode"),
    (0x10, "volatile backup failed"),
    (0x20, "persistent region read-only"),
]


def parse_nvme_smart(d):
    """Decode the 512-byte NVMe SMART/Health log page (NVMe spec figure)."""
    def le(a, b):
        return int.from_bytes(d[a:b], "little")

    sensors = [le(200 + i * 2, 202 + i * 2) for i in range(8)]
    warnings = [label for bit, label in CRITICAL_WARNING_FLAGS if d[0] & bit]
    return {
        "critical_warning": d[0],
        "warnings": warnings,
        "temp_c": le(1, 3) - 273,
        "avail_spare": d[3],
        "spare_thresh": d[4],
        "percent_used": d[5],
        "life_left": max(0, 100 - d[5]),
        "data_read": le(32, 48) * 512000,
        "data_written": le(48, 64) * 512000,
        "host_reads": le(64, 80),
        "host_writes": le(80, 96),
        "busy_minutes": le(96, 112),
        "power_cycles": le(112, 128),
        "power_on_hours": le(128, 144),
        "unsafe_shutdowns": le(144, 160),
        "media_errors": le(160, 176),
        "error_log_entries": le(176, 192),
        "warn_temp_minutes": le(192, 196),
        "crit_temp_minutes": le(196, 200),
        "sensor_temps": [s - 273 for s in sensors if s],
    }


def nvme_smart(ctrl_dev):
    """Read the NVMe SMART/health log page via the admin ioctl (needs root)."""
    try:
        fd = os.open(ctrl_dev, os.O_RDONLY)
    except PermissionError:
        return {"error": "permission denied (run with sudo for full SMART)"}
    except OSError as e:
        return {"error": str(e)}
    try:
        buf = ctypes.create_string_buffer(512)
        cdw10 = ((512 // 4 - 1) << 16) | NVME_LOG_SMART
        cmd = struct.pack(
            "<BBHIIIQQIIIIIIIIII",
            NVME_ADMIN_GET_LOG_PAGE, 0, 0,      # opcode, flags, rsvd
            0xFFFFFFFF, 0, 0,                    # nsid, cdw2, cdw3
            0, ctypes.addressof(buf),            # metadata, addr
            0, 512,                              # metadata_len, data_len
            cdw10, 0, 0, 0, 0, 0,                # cdw10..15
            0, 0,                                # timeout_ms, result
        )
        fcntl.ioctl(fd, NVME_IOCTL_ADMIN_CMD, bytearray(cmd))
        return parse_nvme_smart(buf.raw)
    except OSError as e:
        return {"error": str(e)}
    finally:
        os.close(fd)


SATA_LIFE_ATTRS = ("SSD_Life_Left", "Media_Wearout_Indicator",
                   "Percent_Lifetime_Remain", "Wear_Leveling_Count",
                   "Remaining_Lifetime_Perc")


def smartctl_health(dev):
    """Fallback for SATA drives when smartctl is installed: health verdict
    plus life-left %, reallocated sectors, power-on hours, temperature."""
    if not shutil.which("smartctl"):
        return None
    try:
        out = subprocess.run(
            ["smartctl", "-H", "-A", dev],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    result = {}
    for line in out.splitlines():
        if "overall-health" in line or "SMART Health Status" in line:
            result["verdict"] = line.split(":")[-1].strip()
            continue
        f = line.split()
        if len(f) < 10 or not f[0].isdigit():
            continue
        name, value, raw = f[1], f[3], f[9]
        if name in SATA_LIFE_ATTRS:
            # normalized VALUE column counts down from 100 as the drive wears
            result["life_left"] = int(value)
        elif name == "Reallocated_Sector_Ct":
            result["realloc"] = int(raw)
        elif name == "Power_On_Hours":
            result["power_on_hours"] = int(raw.split("h")[0])
        elif name in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
            result.setdefault("temp_c", int(raw.split()[0]))
        elif name == "Power_Cycle_Count":
            result["power_cycles"] = int(raw)
    return result or None


def hwmon_disk_temps():
    """Controller name (e.g. 'nvme0') or drivetemp block dev -> temperature."""
    temps = {}
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_file(hw + "/name").strip()
        if name not in ("nvme", "drivetemp"):
            continue
        raw = read_file(hw + "/temp1_input").strip()
        if not raw:
            continue
        temp = int(raw) / 1000.0
        real = os.path.realpath(hw + "/device")
        temps[os.path.basename(real)] = temp
    return temps


class DiskSampler:
    def __init__(self):
        self.prev = {}          # dev -> (sectors_read, sectors_written)
        self.prev_time = time.monotonic()
        self.rates = {}         # dev -> (read Bps, write Bps)
        self.sample_rates()

    def sample_rates(self):
        now = time.monotonic()
        elapsed = max(now - self.prev_time, 0.001)
        for line in read_file("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) < 10:
                continue
            dev = f[2]
            sread, swrite = int(f[5]), int(f[9])
            pr, pw = self.prev.get(dev, (sread, swrite))
            self.rates[dev] = (
                (sread - pr) * 512 / elapsed,
                (swrite - pw) * 512 / elapsed,
            )
            self.prev[dev] = (sread, swrite)
        self.prev_time = now

    @staticmethod
    def physical_disks():
        disks = []
        for path in sorted(glob.glob("/sys/block/*")):
            name = os.path.basename(path)
            if name.startswith(("loop", "ram", "zram", "dm-", "sr")):
                continue
            size = int(read_file(path + "/size", "0")) * 512
            model = read_file(path + "/device/model").strip()
            serial = read_file(path + "/device/serial").strip()
            firmware = read_file(path + "/device/firmware_rev").strip()
            rotational = read_file(path + "/queue/rotational", "1").strip() == "1"
            disks.append({
                "name": name,
                "model": model or "?",
                "serial": serial,
                "firmware": firmware,
                "size": size,
                "kind": "HDD" if rotational else "SSD",
            })
        return disks

    @staticmethod
    def mounts():
        seen, rows = set(), []
        for line in read_file("/proc/mounts").splitlines():
            src, mnt, fstype = line.split()[:3]
            if not src.startswith("/dev/") or "loop" in src:
                continue
            if src in seen:
                continue
            seen.add(src)
            mnt_display = mnt.replace("\\040", " ")
            try:
                st = os.statvfs(mnt_display)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total == 0:
                continue
            rows.append({
                "src": src, "mnt": mnt_display, "fstype": fstype,
                "total": total, "used": total - free,
            })
        return rows


def disk_health(disk):
    """Best-effort health info for one physical disk."""
    name = disk["name"]
    if name.startswith("nvme"):
        ctrl = "nvme" + name[4:].split("n")[0]  # nvme0n1 -> nvme0
        smart = nvme_smart(f"/dev/{ctrl}")
        smart["ctrl"] = ctrl
        return smart
    attrs = smartctl_health(f"/dev/{name}")
    return {"sata": attrs} if attrs else {}


def life_projection(percent_used, poh):
    """Rough remaining-lifetime estimate from wear rate so far."""
    if percent_used <= 0 or poh <= 0:
        return "wear <1%, too early to project"
    remaining_h = poh * 100 / percent_used - poh
    years = remaining_h / 8760
    if years > 99:
        return "projected life >99y at current rate"
    return f"projected ~{years:.1f}y left at current write rate"


# ---------------------------------------------------------------- dump mode

def dump_snapshot():
    cpu = CpuSampler()
    procs = ProcessSampler()
    disks_io = DiskSampler()
    time.sleep(1.0)
    cpu.sample()
    procs.sample(cpu.period)
    disks_io.sample_rates()

    mem = mem_info()
    load = os.getloadavg()
    print(f"=== SysVitals {VERSION} snapshot ===")
    print(f"CPU total: {cpu.percents.get('cpu', 0):.1f}%   "
          f"cores: {len(cpu.cores)}   load: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
    t = cpu_temp()
    if t is not None:
        print(f"CPU temp: {t:.0f}°C")
    print(f"Mem: {human_bytes(mem['used'])} / {human_bytes(mem['total'])}   "
          f"Swap: {human_bytes(mem['swap_used'])} / {human_bytes(mem['swap_total'])}")

    print("\n--- Top processes by CPU ---")
    top = sorted(procs.procs, key=lambda p: -p["cpu"])[:10]
    for p in top:
        print(f"{p['pid']:>7} {p['user'][:10]:<10} {p['cpu']:>5.1f}% "
              f"{p['mem']:>4.1f}% {human_bytes(p['rss']):>8} {p['name']}")

    conns = net_connections()
    est = [c for c in conns if c["state"] == "ESTABLISHED"]
    print(f"\n--- Network: {len(conns)} sockets, {len(est)} established ---")
    for c in est[:10]:
        who = f"{c['pid']}/{c['pname']}" if c["pid"] else "-"
        print(f"{c['proto']:<5} {c['laddr'][0]}:{c['laddr'][1]} -> "
              f"{c['raddr'][0]}:{c['raddr'][1]}  {who}")

    print("\n--- Listening ports ---")
    for c in listening_ports(conns):
        who = f"{c['pid']}/{c['pname']}" if c["pid"] else "-"
        svc = port_service(c["laddr"][1], c["proto"])
        print(f"{c['proto']:<5} {c['laddr'][0]}:{c['laddr'][1]:<6} "
              f"{svc:<12} {who}")

    print("\n--- Disks ---")
    temps = hwmon_disk_temps()
    for d in DiskSampler.physical_disks():
        rd, wr = disks_io.rates.get(d["name"], (0, 0))
        print(f"{d['name']}: {d['model']} ({d['kind']}, {human_bytes(d['size'])})  "
              f"R {human_bytes(rd)}/s  W {human_bytes(wr)}/s")
        health = disk_health(d)
        temp = temps.get(health.get("ctrl", d["name"]))
        if temp is not None:
            print(f"  temp: {temp:.0f}°C")
        if "error" in health:
            print(f"  SMART: {health['error']}")
        elif "percent_used" in health:
            ok = health["critical_warning"] == 0
            status = "OK" if ok else "WARNING: " + ", ".join(health["warnings"])
            print(f"  SMART: {status}")
            print(f"  life: {health['life_left']}% remaining "
                  f"(wear {health['percent_used']}%, "
                  f"spare {health['avail_spare']}%)  "
                  f"{life_projection(health['percent_used'], health['power_on_hours'])}")
            print(f"  lifecycle: {health['power_on_hours']}h powered, "
                  f"{health['power_cycles']} cycles, "
                  f"{health['unsafe_shutdowns']} unsafe shutdowns, "
                  f"{health['media_errors']} media errors")
            print(f"  totals: written {human_bytes(health['data_written'])}, "
                  f"read {human_bytes(health['data_read'])}")
        elif health.get("sata"):
            s = health["sata"]
            print(f"  SMART: {s.get('verdict', '?')}"
                  + (f"  life {s['life_left']}%" if "life_left" in s else "")
                  + (f"  POH {s['power_on_hours']}h" if "power_on_hours" in s else ""))
    print("\n--- Mounts ---")
    for m in DiskSampler.mounts():
        pct = 100.0 * m["used"] / m["total"]
        print(f"{m['src']:<20} {m['mnt']:<25} {m['fstype']:<8} "
              f"{human_bytes(m['used'])}/{human_bytes(m['total'])} ({pct:.0f}%)")


# ---------------------------------------------------------------- TUI

TABS = ["Processes", "Network", "Ports", "Disks"]
SORT_KEYS = {"c": "cpu", "m": "mem", "p": "pid", "n": "name", "t": "time"}

C_OK, C_WARN, C_CRIT, C_ACCENT, C_TITLE, C_DIM = 1, 2, 3, 4, 5, 6


class SysVitals:
    def __init__(self, stdscr, interval):
        self.scr = stdscr
        self.interval = interval
        self.tab = 0
        self.sel = 0
        self.scroll = 0
        self.sort = "cpu"
        self.sort_desc = True
        self.show_kthreads = False
        self.mem = mem_info()
        self.search = ""
        self.mode = "normal"        # normal | search | kill
        self.kill_target = None
        self.status = ""
        self.cpu = CpuSampler()
        self.last_cpu_sample = time.monotonic()
        self.procs = ProcessSampler()
        self.disk_io = DiskSampler()
        self.conns = []
        self.rows_data = []
        self.last_refresh = 0.0

        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(C_OK, curses.COLOR_GREEN, -1)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_CRIT, curses.COLOR_RED, -1)
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, -1)
        curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_DIM, curses.COLOR_BLUE, -1)
        self.scr.timeout(200)
        self.refresh_data(force=True)

    # ---------- data ----------

    def refresh_data(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_refresh < self.interval:
            return False
        self.last_refresh = now
        # resample CPU/processes only over windows long enough to be meaningful
        if now - self.last_cpu_sample >= 0.3:
            self.cpu.sample()
            self.mem = mem_info()
            self.procs.sample(self.cpu.period)
            self.last_cpu_sample = now
        if self.tab in (1, 2):
            self.conns = net_connections()
        elif self.tab == 3:
            self.disk_io.sample_rates()
        return True

    # ---------- drawing primitives ----------

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    def bar(self, y, x, width, frac, label, inner=""):
        frac = max(0.0, min(frac, 1.0))
        fill = int(frac * (width - 2))
        color = C_OK if frac < 0.6 else C_WARN if frac < 0.85 else C_CRIT
        self.put(y, x, label, curses.A_BOLD)
        bx = x + len(label)
        self.put(y, bx, "[")
        self.put(y, bx + 1, "|" * fill, curses.color_pair(color))
        text = inner.rjust(width - 2 - fill)
        self.put(y, bx + 1 + fill, text[max(0, len(text) - (width - 2 - fill)):],
                 curses.color_pair(C_DIM))
        self.put(y, bx + width - 1, "]")

    # ---------- header ----------

    def draw_header(self):
        h, w = self.scr.getmaxyx()
        cores = self.cpu.cores
        ncols = 4 if w >= 120 else 2 if w >= 60 else 1
        bar_w = (w - 2) // ncols - 8
        row = 0
        for i, core in enumerate(cores):
            pct = self.cpu.percents[core]
            y = row + i // ncols
            x = 1 + (i % ncols) * ((w - 2) // ncols)
            self.bar(y, x, bar_w, pct / 100.0, f"{i:>2}", f"{pct:4.1f}%")
        row += (len(cores) + ncols - 1) // ncols

        mem = self.mem
        half = (w - 2) // 2
        self.bar(row, 1, half - 8, mem["used"] / mem["total"], "Mem",
                 f"{human_bytes(mem['used'])}/{human_bytes(mem['total'])}")
        if mem["swap_total"]:
            self.bar(row, 1 + half, half - 8,
                     mem["swap_used"] / mem["swap_total"], "Swp",
                     f"{human_bytes(mem['swap_used'])}/{human_bytes(mem['swap_total'])}")
        else:
            self.put(row, 1 + half, "Swp [none]", curses.color_pair(C_DIM))
        row += 1

        load = os.getloadavg()
        up = float(read_file("/proc/uptime", "0 0").split()[0])
        days, rem = divmod(int(up), 86400)
        temp = cpu_temp()
        tstr = f"  CPU {temp:.0f}°C" if temp is not None else ""
        total = self.cpu.percents.get("cpu", 0.0)
        p = self.procs
        ntasks = len(p.procs) - p.n_kthreads
        info = (f" CPU {total:4.1f}%{tstr}  "
                f"Tasks {ntasks}, {p.n_threads} thr, {p.n_kthreads} kthr; "
                f"{p.n_running} running  "
                f"Load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}  "
                f"Up {days}d {rem // 3600:02d}:{rem % 3600 // 60:02d}")
        self.put(row, 0, info, curses.color_pair(C_ACCENT) | curses.A_BOLD)
        row += 1

        x = 0
        for i, name in enumerate(TABS):
            label = f" {i + 1}:{name} "
            attr = (curses.color_pair(C_TITLE) | curses.A_BOLD
                    if i == self.tab else curses.color_pair(C_DIM))
            self.put(row, x, label, attr)
            x += len(label) + 1
        self.put(row, x + 1, f"Ω SysVitals {VERSION}", curses.color_pair(C_DIM))
        return row + 1

    # ---------- tab bodies ----------

    def build_rows(self):
        """Return (header, [(text, attr, meta)]) for the active tab."""
        q = self.search.lower()
        if self.tab == 0:
            hdr = (f"{'PID':>7} {'USER':<10} {'S':1} {'CPU%':>6} {'MEM%':>5} "
                   f"{'RSS':>8} {'THR':>4} {'TIME':>7}  COMMAND")
            procs = self.procs.procs
            if not self.show_kthreads:
                procs = [p for p in procs if not p["kthread"]]
            if q:
                procs = [p for p in procs
                         if q in p["name"].lower() or q in p["cmd"].lower()
                         or q == str(p["pid"])]
            procs = sorted(procs, key=lambda p: p[self.sort],
                           reverse=self.sort_desc)
            rows = []
            for p in procs:
                attr = 0
                if p["cpu"] > 50:
                    attr = curses.color_pair(C_WARN)
                text = (f"{p['pid']:>7} {p['user'][:10]:<10} {p['state']:1} "
                        f"{p['cpu']:>6.1f} {p['mem']:>5.1f} "
                        f"{human_bytes(p['rss']):>8} {p['threads']:>4} "
                        f"{human_time(p['time']):>7}  {p['cmd']}")
                rows.append((text, attr, p))
            return hdr, rows

        if self.tab == 1:
            hdr = (f"{'PROTO':<6} {'LOCAL':<25} {'REMOTE':<25} "
                   f"{'STATE':<12} {'USER':<10} PID/PROGRAM")
            rows = []
            order = {"ESTABLISHED": 0, "LISTEN": 2}
            conns = sorted(self.conns,
                           key=lambda c: (order.get(c["state"], 1), c["proto"]))
            for c in conns:
                who = f"{c['pid']}/{c['pname']}" if c["pid"] else "-"
                laddr = f"{c['laddr'][0]}:{c['laddr'][1]}"
                raddr = f"{c['raddr'][0]}:{c['raddr'][1]}"
                text = (f"{c['proto']:<6} {laddr:<25} {raddr:<25} "
                        f"{c['state']:<12} {c['user'][:10]:<10} {who}")
                if q and q not in text.lower():
                    continue
                attr = (curses.color_pair(C_OK) if c["state"] == "ESTABLISHED"
                        else curses.color_pair(C_ACCENT) if c["state"] == "LISTEN"
                        else 0)
                rows.append((text, attr, c))
            return hdr, rows

        if self.tab == 2:
            hdr = (f"{'PROTO':<6} {'ADDRESS':<30} {'PORT':>6} "
                   f"{'SERVICE':<14} {'USER':<10} PID/PROGRAM")
            rows = []
            for c in listening_ports(self.conns):
                who = f"{c['pid']}/{c['pname']}" if c["pid"] else "-"
                svc = port_service(c["laddr"][1], c["proto"]) or "-"
                exposed = c["laddr"][0] in ("0.0.0.0", "::")
                text = (f"{c['proto']:<6} {c['laddr'][0]:<30} "
                        f"{c['laddr'][1]:>6} {svc:<14} "
                        f"{c['user'][:10]:<10} {who}")
                if q and q not in text.lower():
                    continue
                attr = curses.color_pair(C_WARN) if exposed else 0
                rows.append((text, attr, c))
            return hdr, rows

        # Disks tab: free-form rows, no selection semantics
        rows = []
        temps = hwmon_disk_temps()

        def life_bar_row(life, label="Health"):
            width = 20
            fill = int(life / 100 * width)
            color = (C_OK if life > 30 else C_WARN if life > 10 else C_CRIT)
            return (f"    {label}: [{'|' * fill}{' ' * (width - fill)}] "
                    f"{life}% life remaining", curses.color_pair(color), None)

        for d in DiskSampler.physical_disks():
            rd, wr = self.disk_io.rates.get(d["name"], (0, 0))
            extra = f"  fw {d['firmware']}" if d["firmware"] else ""
            rows.append((
                f"▸ /dev/{d['name']}  {d['model']}{extra}  {d['kind']}  "
                f"{human_bytes(d['size'])}   R {human_bytes(rd)}/s  "
                f"W {human_bytes(wr)}/s",
                curses.color_pair(C_ACCENT) | curses.A_BOLD, None))
            if d["serial"]:
                rows.append((f"    Serial {d['serial']}",
                             curses.color_pair(C_DIM), None))
            health = disk_health(d)
            temp = temps.get(health.get("ctrl", d["name"]))
            if temp is not None and "temp_c" not in health:
                health["temp_c"] = temp
            if "error" in health:
                if temp is not None:
                    rows.append((f"    Temperature: {temp:.0f}°C", 0, None))
                rows.append((f"    SMART: {health['error']}",
                             curses.color_pair(C_DIM), None))
            elif "percent_used" in health:
                ok = health["critical_warning"] == 0
                rows.append((
                    f"    SMART: {'HEALTHY' if ok else 'CRITICAL'}"
                    + (f" — {', '.join(health['warnings'])}" if not ok else ""),
                    curses.color_pair(C_OK if ok else C_CRIT)
                    | (curses.A_BOLD if not ok else 0), None))
                rows.append(life_bar_row(health["life_left"]))
                rows.append((
                    f"    Wear used {health['percent_used']}%   "
                    f"spare {health['avail_spare']}% "
                    f"(alarm at {health['spare_thresh']}%)   "
                    f"{life_projection(health['percent_used'], health['power_on_hours'])}",
                    0, None))
                rows.append((
                    f"    Lifecycle: power-on {health['power_on_hours']}h"
                    f"   cycles {health['power_cycles']}"
                    f"   unsafe shutdowns {health['unsafe_shutdowns']}"
                    f"   media errors {health['media_errors']}"
                    f"   error-log {health['error_log_entries']}", 0, None))
                rows.append((
                    f"    Totals: written {human_bytes(health['data_written'])}"
                    f"   read {human_bytes(health['data_read'])}"
                    f"   busy {health['busy_minutes']}min"
                    f"   cmds {health['host_writes'] // 10**6}M wr"
                    f" / {health['host_reads'] // 10**6}M rd", 0, None))
                sensors = "/".join(f"{t}°" for t in health["sensor_temps"])
                overtemp = (f"   over warn-temp {health['warn_temp_minutes']}min"
                            if health["warn_temp_minutes"] else "")
                tc = health["temp_c"]
                tattr = (curses.color_pair(C_OK) if tc < 55
                         else curses.color_pair(C_WARN) if tc < 70
                         else curses.color_pair(C_CRIT))
                rows.append((f"    Temperature: {tc}°C"
                             + (f"  (sensors {sensors})" if sensors else "")
                             + overtemp, tattr, None))
            elif health.get("sata"):
                s = health["sata"]
                verdict = s.get("verdict", "?")
                good = verdict.upper() in ("PASSED", "OK")
                rows.append((f"    SMART: {verdict}",
                             curses.color_pair(C_OK if good else C_CRIT), None))
                if "life_left" in s:
                    rows.append(life_bar_row(s["life_left"]))
                bits = []
                if "power_on_hours" in s:
                    bits.append(f"power-on {s['power_on_hours']}h")
                if "power_cycles" in s:
                    bits.append(f"cycles {s['power_cycles']}")
                if "realloc" in s:
                    bits.append(f"reallocated sectors {s['realloc']}")
                if "temp_c" in s:
                    bits.append(f"{s['temp_c']}°C")
                if bits:
                    rows.append(("    " + "   ".join(bits), 0, None))
            else:
                if temp is not None:
                    rows.append((f"    Temperature: {temp:.0f}°C", 0, None))
                rows.append(("    SMART: unavailable "
                             "(install smartmontools for SATA health)",
                             curses.color_pair(C_DIM), None))
            rows.append(("", 0, None))
        rows.append(("▸ Filesystems", curses.color_pair(C_ACCENT) | curses.A_BOLD,
                     None))
        for m in DiskSampler.mounts():
            frac = m["used"] / m["total"]
            width = 20
            fill = int(frac * width)
            color = C_OK if frac < 0.7 else C_WARN if frac < 0.9 else C_CRIT
            bar = "|" * fill + " " * (width - fill)
            rows.append((
                f"    {m['src']:<18} {m['mnt']:<22.22} {m['fstype']:<8} "
                f"[{bar}] {frac * 100:3.0f}%  "
                f"{human_bytes(m['used'])} / {human_bytes(m['total'])}",
                curses.color_pair(color), None))
        return "", rows

    def draw_body(self, top):
        h, w = self.scr.getmaxyx()
        hdr, rows = self.build_rows()
        self.rows_data = rows
        body_h = h - top - 1
        y = top
        if hdr:
            self.put(y, 0, hdr.ljust(w - 1), curses.color_pair(C_TITLE))
            y += 1
            body_h -= 1
        self.sel = max(0, min(self.sel, len(rows) - 1))
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + body_h:
            self.scroll = self.sel - body_h + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - body_h)))
        selectable = self.tab != 3
        for i in range(self.scroll, min(len(rows), self.scroll + body_h)):
            text, attr, _ = rows[i]
            if selectable and i == self.sel:
                attr = curses.A_REVERSE
            self.put(y + i - self.scroll, 0, text.ljust(w - 1), attr)

    def draw_footer(self):
        h, w = self.scr.getmaxyx()
        if self.mode == "search":
            text = f"Search: {self.search}▏  (Enter apply, Esc clear)"
            self.put(h - 1, 0, text.ljust(w - 1), curses.color_pair(C_WARN))
            return
        if self.mode == "kill" and self.kill_target:
            p = self.kill_target
            text = (f"Signal PID {p['pid']} ({p['name']})?  "
                    f"[t] SIGTERM  [k] SIGKILL  [Esc] cancel")
            self.put(h - 1, 0, text.ljust(w - 1),
                     curses.color_pair(C_CRIT) | curses.A_BOLD)
            return
        hints = {
            0: "q Quit  Tab/1-4 Tabs  ↑↓ Move  / Filter  c/m/p/n/t Sort  K Kthreads  F9/x Kill",
            1: "q Quit  Tab/1-4 Tabs  ↑↓ Move  / Filter    (green=established, cyan=listen)",
            2: "q Quit  Tab/1-4 Tabs  ↑↓ Move  / Filter    (yellow = exposed on all interfaces)",
            3: "q Quit  Tab/1-4 Tabs  ↑↓ Scroll    (sudo sysvitals for full NVMe SMART)",
        }
        text = hints[self.tab]
        if self.search:
            text += f"   filter:'{self.search}'"
        if self.status:
            text += f"   {self.status}"
        self.put(h - 1, 0, text.ljust(w - 1), curses.color_pair(C_DIM))

    # ---------- input ----------

    def handle_key(self, ch):
        if self.mode == "search":
            if ch in (27,):
                self.search, self.mode = "", "normal"
            elif ch in (curses.KEY_ENTER, 10, 13):
                self.mode = "normal"
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.search = self.search[:-1]
            elif 32 <= ch < 127:
                self.search += chr(ch)
            return True
        if self.mode == "kill":
            if ch in (ord("t"), ord("k")):
                sig = signal.SIGTERM if ch == ord("t") else signal.SIGKILL
                p = self.kill_target
                try:
                    os.kill(p["pid"], sig)
                    self.status = f"sent {sig.name} to {p['pid']}"
                except OSError as e:
                    self.status = f"kill failed: {e.strerror}"
                self.last_refresh = 0
            self.mode, self.kill_target = "normal", None
            return True

        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("\t"):
            self.switch_tab((self.tab + 1) % len(TABS))
        elif ch in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.switch_tab(ch - ord("1"))
        elif curses.KEY_F1 <= ch <= curses.KEY_F4:
            self.switch_tab(ch - curses.KEY_F1)
        elif ch == curses.KEY_UP:
            self.sel -= 1
        elif ch == curses.KEY_DOWN:
            self.sel += 1
        elif ch == curses.KEY_PPAGE:
            self.sel -= 20
        elif ch == curses.KEY_NPAGE:
            self.sel += 20
        elif ch == curses.KEY_HOME:
            self.sel = 0
        elif ch == curses.KEY_END:
            self.sel = 10 ** 9
        elif ch == ord("/"):
            self.mode = "search"
        elif self.tab == 0 and ch in map(ord, SORT_KEYS):
            key = SORT_KEYS[chr(ch)]
            if self.sort == key:
                self.sort_desc = not self.sort_desc
            else:
                self.sort = key
                self.sort_desc = key in ("cpu", "mem", "time")
        elif self.tab == 0 and ch == ord("K"):
            self.show_kthreads = not self.show_kthreads
        elif self.tab == 0 and ch in (curses.KEY_F9, ord("x")):
            if 0 <= self.sel < len(self.rows_data):
                meta = self.rows_data[self.sel][2]
                if meta:
                    self.kill_target = meta
                    self.mode = "kill"
        self.sel = max(0, self.sel)
        return True

    def switch_tab(self, tab):
        if tab != self.tab:
            self.tab = tab
            self.sel = self.scroll = 0
            self.search = ""
            self.status = ""
            self.refresh_data(force=True)

    # ---------- main loop ----------

    def run(self):
        dirty = True
        while True:
            if self.refresh_data():
                dirty = True
            if dirty:
                self.scr.erase()
                top = self.draw_header()
                self.draw_body(top)
                self.draw_footer()
                self.scr.refresh()
                dirty = False
            ch = self.scr.getch()
            if ch == -1:
                continue
            dirty = True
            if ch == curses.KEY_RESIZE:
                continue
            if not self.handle_key(ch):
                break


def main():
    parser = argparse.ArgumentParser(
        prog="sysvitals",
        description="htop + netstat + open ports + SSD health in one tool")
    parser.add_argument("-i", "--interval", type=float, default=1.5,
                        help="refresh interval in seconds (default 1.5, same as htop)")
    parser.add_argument("--dump", action="store_true",
                        help="print one text snapshot and exit (no TUI)")
    parser.add_argument("--version", action="version",
                        version=f"sysvitals {VERSION}")
    args = parser.parse_args()

    if args.dump:
        dump_snapshot()
        return

    if not sys.stdout.isatty():
        sys.exit("sysvitals: not a terminal (use --dump for text output)")
    try:
        curses.wrapper(lambda scr: SysVitals(scr, args.interval).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
