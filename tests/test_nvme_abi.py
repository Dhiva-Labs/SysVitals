#!/usr/bin/env python3
"""ABI checks for the NVMe admin ioctl and the /proc/net address decoding.

The pure-Python checks run everywhere. The UAPI check compiles a tiny C program
against the kernel's own <linux/nvme_ioctl.h> and compares the struct layout and
the ioctl number with what SysVitals builds, so a layout assumption that holds on
x86-64 but not on another architecture fails on that architecture. CI sets
SYSVITALS_REQUIRE_UAPI_CHECK=1 so a missing compiler or header is a failure
instead of a skip.
"""
import importlib.util
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "sysvitals", os.path.join(ROOT, "sysvitals.py"))
sv = importlib.util.module_from_spec(spec)
sys.modules["sysvitals"] = sv
spec.loader.exec_module(sv)

# struct nvme_admin_cmd, in declaration order: (field, struct format char)
FIELDS = [
    ("opcode", "B"), ("flags", "B"), ("rsvd1", "H"), ("nsid", "I"),
    ("cdw2", "I"), ("cdw3", "I"), ("metadata", "Q"), ("addr", "Q"),
    ("metadata_len", "I"), ("data_len", "I"),
    ("cdw10", "I"), ("cdw11", "I"), ("cdw12", "I"),
    ("cdw13", "I"), ("cdw14", "I"), ("cdw15", "I"),
    ("timeout_ms", "I"), ("result", "I"),
]


def packed_offsets():
    """Field offsets if the fields are laid out back to back, no padding."""
    offsets, pos = {}, 0
    for name, ch in FIELDS:
        offsets[name] = pos
        pos += struct.calcsize("=" + ch)
    offsets["__size__"] = pos
    return offsets


C_SOURCE = r"""
#include <stddef.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <linux/nvme_ioctl.h>

#define OFF(f) printf(#f "=%zu\n", offsetof(struct nvme_admin_cmd, f))

int main(void) {
    printf("__size__=%zu\n", sizeof(struct nvme_admin_cmd));
    printf("__ioctl__=%lu\n", (unsigned long)NVME_IOCTL_ADMIN_CMD);
    OFF(opcode); OFF(flags); OFF(rsvd1); OFF(nsid); OFF(cdw2); OFF(cdw3);
    OFF(metadata); OFF(addr); OFF(metadata_len); OFF(data_len);
    OFF(cdw10); OFF(cdw11); OFF(cdw12); OFF(cdw13); OFF(cdw14); OFF(cdw15);
    OFF(timeout_ms); OFF(result);
    return 0;
}
"""


class NvmeAdminCmdLayoutTest(unittest.TestCase):
    def test_format_matches_field_list(self):
        self.assertEqual(sv.NVME_ADMIN_CMD_FMT, "=" + "".join(c for _, c in FIELDS))

    def test_size_is_72_bytes(self):
        self.assertEqual(struct.calcsize(sv.NVME_ADMIN_CMD_FMT), 72)
        self.assertEqual(len(sv.nvme_smart_cmd(0)), 72)

    @unittest.skipIf(
        platform.machine().startswith(("ppc", "mips", "sparc", "alpha", "parisc")),
        "these architectures use a different _IOC bit layout")
    def test_ioctl_number_is_iowr_N_0x41_size_72(self):
        generic_iowr = (3 << 30) | (72 << 16) | (ord("N") << 8) | 0x41
        self.assertEqual(sv.NVME_IOCTL_ADMIN_CMD, generic_iowr)

    def test_smart_command_fields(self):
        addr = 0x1122334455667788
        cmd = sv.nvme_smart_cmd(addr)
        off = packed_offsets()

        def field(name, ch):
            return struct.unpack_from("=" + ch, cmd, off[name])[0]

        self.assertEqual(field("opcode", "B"), 0x02)         # Get Log Page
        self.assertEqual(field("flags", "B"), 0)
        self.assertEqual(field("nsid", "I"), 0xFFFFFFFF)     # controller-wide log
        self.assertEqual(field("metadata", "Q"), 0)
        self.assertEqual(field("addr", "Q"), addr)
        self.assertEqual(field("metadata_len", "I"), 0)
        self.assertEqual(field("data_len", "I"), 512)
        # NUMD (dwords - 1) in bits 31:16, log page identifier 0x02 in 7:0
        self.assertEqual(field("cdw10", "I"), (127 << 16) | 0x02)
        self.assertEqual(field("timeout_ms", "I"), 0)
        self.assertEqual(field("result", "I"), 0)

    def test_ioctl_call_reaches_the_kernel_with_a_valid_request_number(self):
        # /dev/null does not implement the NVMe ioctl, so a well-formed request
        # comes back as OSError (ENOTTY) which nvme_smart reports as an error
        # entry. A bad request number or argument type would raise something else.
        result = sv.nvme_smart("/dev/null")
        self.assertIn("error", result)

    def test_layout_matches_kernel_uapi_header(self):
        required = os.environ.get("SYSVITALS_REQUIRE_UAPI_CHECK") == "1"
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc or not os.path.exists("/usr/include/linux/nvme_ioctl.h"):
            if required:
                self.fail("C compiler or <linux/nvme_ioctl.h> missing")
            self.skipTest("C compiler or <linux/nvme_ioctl.h> not available")
        with tempfile.TemporaryDirectory() as tmp:
            src, exe = os.path.join(tmp, "abi.c"), os.path.join(tmp, "abi")
            with open(src, "w") as f:
                f.write(C_SOURCE)
            build = subprocess.run([cc, "-o", exe, src],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            out = subprocess.run([exe], capture_output=True, text=True,
                                 check=True).stdout
        c = {k: int(v) for k, v in (line.split("=") for line in out.split())}
        print(f"\n{platform.machine()}: kernel header says size={c['__size__']} "
              f"ioctl=0x{c['__ioctl__']:X}", file=sys.stderr)
        self.assertEqual(c.pop("__ioctl__"), sv.NVME_IOCTL_ADMIN_CMD)
        self.assertEqual(c, packed_offsets())


def host_order_word(packed4):
    """How the kernel prints one 32-bit address word in /proc/net/*."""
    return "%08X" % struct.unpack("=I", packed4)[0]


class DecodeAddrTest(unittest.TestCase):
    def test_ipv4(self):
        for ip in ("127.0.0.1", "10.0.2.15", "192.168.1.200", "0.0.0.0",
                   "255.255.255.255"):
            field = "%s:%04X" % (
                host_order_word(socket.inet_pton(socket.AF_INET, ip)), 8080)
            self.assertEqual(sv._decode_addr(field, socket.AF_INET), (ip, 8080))

    def test_ipv6(self):
        for ip in ("::1", "::", "fe80::1", "2001:db8::42",
                   "2a00:1450:4009:81f::200e"):
            packed = socket.inet_pton(socket.AF_INET6, ip)
            addr = "".join(host_order_word(packed[i:i + 4]) for i in range(0, 16, 4))
            self.assertEqual(
                sv._decode_addr(addr + ":01BB", socket.AF_INET6), (ip, 443))

    def test_ipv4_mapped_ipv6_is_shown_as_ipv4(self):
        packed = socket.inet_pton(socket.AF_INET6, "::ffff:10.1.2.3")
        addr = "".join(host_order_word(packed[i:i + 4]) for i in range(0, 16, 4))
        self.assertEqual(
            sv._decode_addr(addr + ":0016", socket.AF_INET6), ("10.1.2.3", 22))

    @unittest.skipUnless(sys.byteorder == "little", "literal is a little-endian dump")
    def test_literal_proc_net_values_on_little_endian(self):
        self.assertEqual(
            sv._decode_addr("0100007F:0050", socket.AF_INET), ("127.0.0.1", 80))
        self.assertEqual(
            sv._decode_addr("00000000000000000000000001000000:1F90",
                            socket.AF_INET6), ("::1", 8080))

    def test_malformed_address_raises_value_error(self):
        with self.assertRaises(ValueError):
            sv._decode_addr("0100:0050", socket.AF_INET)
        with self.assertRaises(ValueError):
            sv._decode_addr("0100007F:0050", socket.AF_INET6)
        with self.assertRaises(ValueError):
            sv._decode_addr("ZZZZZZZZ:0050", socket.AF_INET)


if __name__ == "__main__":
    unittest.main()
