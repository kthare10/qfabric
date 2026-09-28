# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Stand-alone BMv2 check of per-photon thinning — no PTF needed.

Runs anywhere `simple_switch` + `ip` are available and raw sockets are allowed
(a Linux box, or the QFabric BMv2 Docker image with --privileged):

    python3 p4/tests/bmv2_multiphoton_check.py --json p4/bmv2/quantum_channel.json

It creates veth0<->veth1 and veth2<->veth3, starts simple_switch on veth1/veth3
with the compiled program, installs a wavelength-0 entry with P(loss) = 0.5 and
the classical/port tables, then sends N pulse frames with photon_count = n and
checks the surviving-count histogram against Binomial(n, 0.5):

  * a frame is forwarded iff at least one photon survives  (P = 1 - 0.5^n)
  * the forwarded frame carries the surviving count        (mean over sent = n/2)
  * a legacy frame (count byte 0) behaves exactly like n = 1 (P = 0.5)

Exit status 0 = every check within 4 sigma; the histogram is printed either way.
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import subprocess
import sys
import time

ETHERTYPE_PHOTON = 0x7101
ALICE_MAC = bytes.fromhex("020000000001")
BOB_MAC = bytes.fromhex("020000000002")
SW_A = bytes.fromhex("020000000011")
SW_B = bytes.fromhex("020000000012")
THRIFT = 9099


def sh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def frame(seq: int, count: int) -> bytes:
    hdr = BOB_MAC + ALICE_MAC + struct.pack("!H", ETHERTYPE_PHOTON)
    photon = struct.pack("!4B3IB", 1, 0, 0, 0, seq, 0, 0, count)
    return (hdr + photon).ljust(60, b"\x00")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--pulses", type=int, default=4000)
    ap.add_argument("--loss", type=float, default=0.5)
    args = ap.parse_args()

    for a, b in (("veth0", "veth1"), ("veth2", "veth3")):
        sh(f"ip link del {a} 2>/dev/null", check=False)
        sh(f"ip link add {a} type veth peer name {b}")
        for dev in (a, b):
            sh(f"ip link set {dev} up && ip link set {dev} mtu 1500")
            sh(f"ethtool -K {dev} rx off tx off 2>/dev/null", check=False)
    sw = subprocess.Popen(
        ["simple_switch", "-i", "0@veth1", "-i", "1@veth3", "--thrift-port", str(THRIFT),
         "--log-level", "warn", args.json],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        threshold = min(int(args.loss * 2**32), 2**32 - 1)
        cli = "\n".join([
            f"table_add quantum_channel_params set_channel_params 0 => {threshold} 1 "
            f"{SW_B.hex(':')} {BOB_MAC.hex(':')}",
            f"table_add port_forwarding port_forward 0 => 1 {SW_B.hex(':')} {BOB_MAC.hex(':')}",
            f"table_add port_forwarding port_forward 1 => 0 {SW_A.hex(':')} {ALICE_MAC.hex(':')}",
            f"table_add classical_channel_params classical_forward 0 => 1 {SW_B.hex(':')} {BOB_MAC.hex(':')}",
            f"table_add classical_channel_params classical_forward 1 => 0 {SW_A.hex(':')} {ALICE_MAC.hex(':')}",
        ]) + "\n"
        out = subprocess.run(["simple_switch_CLI", "--thrift-port", str(THRIFT)],
                             input=cli, capture_output=True, text=True)
        if "Error" in out.stdout or out.returncode != 0:
            print(out.stdout, out.stderr)
            return 2

        tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE_PHOTON))
        tx.bind(("veth0", 0))
        rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE_PHOTON))
        rx.bind(("veth2", 0))
        rx.settimeout(0.5)

        ok = True
        for n in (0, 1, 3, 8):
            eff_n = max(n, 1)
            for seq in range(args.pulses):
                tx.send(frame(seq, n))
                if seq % 200 == 199:
                    time.sleep(0.01)            # do not overrun BMv2
            time.sleep(1.0)
            hist: dict[int, int] = {}
            got = 0
            while True:
                try:
                    f = rx.recv(2048)
                except socket.timeout:
                    break
                if f[12:14] != struct.pack("!H", ETHERTYPE_PHOTON):
                    continue
                count = f[14 + 16]
                hist[count] = hist.get(count, 0) + 1
                got += 1
            p_fwd = 1.0 - args.loss ** eff_n
            exp_fwd = args.pulses * p_fwd
            sigma_fwd = math.sqrt(args.pulses * p_fwd * (1 - p_fwd)) or 1.0
            mean_surv = sum(k * v for k, v in hist.items()) / args.pulses
            exp_mean = eff_n * (1 - args.loss)
            sigma_mean = math.sqrt(eff_n * args.loss * (1 - args.loss) / args.pulses)
            good = (abs(got - exp_fwd) <= 4 * sigma_fwd
                    and abs(mean_surv - exp_mean) <= 4 * sigma_mean
                    and all(1 <= k <= eff_n for k in hist))
            ok &= good
            print(f"count byte {n}: forwarded {got}/{args.pulses} (expect {exp_fwd:.0f} "
                  f"± {4 * sigma_fwd:.0f}); survivors histogram {dict(sorted(hist.items()))}; "
                  f"mean survivors/sent {mean_surv:.3f} (expect {exp_mean:.3f}) "
                  f"-> {'PASS' if good else 'FAIL'}")
        print("ALL PASS" if ok else "SOME FAILED")
        return 0 if ok else 1
    finally:
        sw.terminate()
        for dev in ("veth0", "veth2"):
            sh(f"ip link del {dev} 2>/dev/null", check=False)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("needs root (veth + raw sockets)", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
