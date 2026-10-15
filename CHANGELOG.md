# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- CI runs the tests and `--dump` on Linux x64 and Linux arm64.
- Tests that compare the NVMe admin command layout and ioctl number with the
  kernel's `<linux/nvme_ioctl.h>` and cover the `/proc/net` address decoding.

### Changed

- The NVMe admin command is packed in native byte order (it was forced to
  little-endian) and the `/proc/net` address decoder no longer assumes a
  little-endian host. Output is unchanged on x86-64 and arm64.

## [1.0.0] - 2026-07-22

### Added

- **Processes tab**: htop-style process table with per-core CPU bars, memory
  and swap usage, CPU temperature, load average and uptime. Sortable by CPU,
  memory, PID, name, or time; live search filter; kill via SIGTERM/SIGKILL.
  Kernel threads are counted separately and hidden by default, matching
  htop's semantics.
- **Network tab**: every TCP/UDP socket (IPv4 and IPv6) with connection
  state, owning user, and owning PID/program — a `netstat -tunp` equivalent
  read directly from `/proc/net`.
- **Ports tab**: listening ports with resolved service names; ports exposed
  on all interfaces are highlighted as a security cue.
- **Disks tab**: drive model/serial/firmware, live read/write throughput,
  and SSD health —
  - NVMe: SMART/health log read directly via the kernel admin ioctl,
    including life-remaining percentage, wear used, spare capacity vs.
    alarm threshold, projected remaining lifetime, decoded critical
    warnings, lifecycle counters (power-on hours, power cycles, unsafe
    shutdowns, media errors), and per-sensor temperatures.
  - SATA: falls back to `smartctl -H -A` when smartmontools is installed.
  - Filesystem usage bars for all mounted drives.
- `--dump` mode: a one-shot plain-text snapshot of all four views, useful
  for scripts or SSH one-liners.
- Zero third-party Python dependencies — pure standard library only.

### Notes

- CPU and memory figures are computed with the same formulas htop uses
  (tick-delta ratios from `/proc/stat`, `MemAvailable`-based memory
  accounting), so readings track closely with htop's own numbers.
- Full NVMe SMART detail requires running with `sudo`; without it, drive
  temperature and I/O throughput are still shown via hwmon and
  `/proc/diskstats`.

[1.0.0]: https://github.com/Dhiva-Labs/SysVitals/releases/tag/v1.0.0
