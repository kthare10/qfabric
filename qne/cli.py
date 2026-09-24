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

"""CLI entry points for QFabric Alice and Bob nodes.

Usage:
    python -m qne.cli alice --config scenario.yml --interface veth1 --bob-host 127.0.0.1
    python -m qne.cli bob   --config scenario.yml --interface veth3
"""

from __future__ import annotations

import argparse
import os
import sys

from qne.config import ScenarioConfig


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qfabric",
        description="QFabric Quantum Node Emulator — BB84 QKD on FABRIC",
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    # Alice subcommand
    alice_parser = subparsers.add_parser("alice", help="Run as Alice (photon source)")
    alice_parser.add_argument(
        "--config", "-c", required=True, help="Path to scenario YAML config"
    )
    alice_parser.add_argument(
        "--interface", "-i", default="veth1", help="Raw socket interface (default: veth1)"
    )
    alice_parser.add_argument(
        "--bob-host", default="127.0.0.1", help="Bob's IP for classical channel"
    )
    alice_parser.add_argument(
        "--bob-port", type=int, default=5100, help="Bob's classical channel port"
    )
    alice_parser.add_argument(
        "--dst-mac", default=None,
        help="Destination MAC for photon frames (colon-separated hex, e.g., AA:BB:CC:DD:EE:FF)",
    )
    alice_parser.add_argument(
        "--src-mac", default=None,
        help="Source MAC for photon frames (colon-separated hex)",
    )
    alice_parser.add_argument(
        "--auth-key", default=None,
        help="pre-shared key: HMAC-authenticate the classical channel "
             "(visible in `ps`; prefer --auth-key-file or $QNE_AUTH_KEY)",
    )
    alice_parser.add_argument(
        "--auth-key-file", default=None,
        help="read the pre-shared authentication key from this file",
    )
    alice_parser.add_argument(
        "--output", "-o", help="Output JSON file for metrics"
    )

    # Bob subcommand
    bob_parser = subparsers.add_parser("bob", help="Run as Bob (detector)")
    bob_parser.add_argument(
        "--config", "-c", required=True, help="Path to scenario YAML config"
    )
    bob_parser.add_argument(
        "--interface", "-i", default="veth3", help="Raw socket interface (default: veth3)"
    )
    bob_parser.add_argument(
        "--host", default="0.0.0.0", help="Classical channel listen host"
    )
    bob_parser.add_argument(
        "--port", type=int, default=5100, help="Classical channel listen port"
    )
    bob_parser.add_argument(
        "--auth-key", default=None,
        help="pre-shared key: HMAC-authenticate the classical channel "
             "(visible in `ps`; prefer --auth-key-file or $QNE_AUTH_KEY)",
    )
    bob_parser.add_argument(
        "--auth-key-file", default=None,
        help="read the pre-shared authentication key from this file",
    )
    bob_parser.add_argument(
        "--no-reconcile", dest="reconcile", action="store_false",
        help="skip Cascade error reconciliation + privacy amplification",
    )
    bob_parser.add_argument(
        "--finite-key", action="store_true",
        help="size privacy amplification with the finite-key bound (Serfling/TLGR) "
             "instead of the asymptotic 1-h(Q) accounting",
    )
    bob_parser.add_argument("--eps-sec", type=float, default=1e-9,
                            help="finite-key security parameter (default 1e-9)")
    bob_parser.add_argument("--eps-cor", type=float, default=1e-15,
                            help="correctness parameter for key verification (default 1e-15)")
    bob_parser.add_argument(
        "--accept-timeout", type=float, default=None,
        help="seconds to wait for Alice to connect (default: forever)",
    )
    bob_parser.add_argument(
        "--output", "-o", help="Output JSON file for metrics"
    )

    return parser


def resolve_auth_key(args) -> str | bytes | None:
    """Pre-shared key from --auth-key-file, else $QNE_AUTH_KEY, else --auth-key."""
    if getattr(args, "auth_key_file", None):
        with open(args.auth_key_file, "rb") as f:
            return f.read().strip()
    env = os.environ.get("QNE_AUTH_KEY")
    if env:
        return env
    return args.auth_key


def main_alice() -> None:
    """Entry point for Alice."""
    parser = create_parser()
    # Inject 'alice' as the role when called via entry point
    args = parser.parse_args(["alice"] + sys.argv[1:])
    _run_alice(args)


def main_bob() -> None:
    """Entry point for Bob."""
    parser = create_parser()
    args = parser.parse_args(["bob"] + sys.argv[1:])
    _run_bob(args)


def _mac_str_to_bytes(mac_str: str | None) -> bytes | None:
    """Convert colon-separated MAC string to 6 bytes."""
    if mac_str is None:
        return None
    return bytes(int(b, 16) for b in mac_str.split(":"))


def _run_alice(args: argparse.Namespace) -> None:
    from qne.alice import Alice

    config = ScenarioConfig.from_yaml(args.config)
    alice = Alice(
        config=config,
        interface=args.interface,
        bob_host=args.bob_host,
        bob_port=args.bob_port,
        dst_mac=_mac_str_to_bytes(getattr(args, "dst_mac", None)),
        src_mac=_mac_str_to_bytes(getattr(args, "src_mac", None)),
        auth_key=resolve_auth_key(args),
    )
    metrics = alice.run()
    if args.output:
        metrics.to_json(args.output)
        print(f"Metrics saved to {args.output}")


def _run_bob(args: argparse.Namespace) -> None:
    from qne.bob import Bob

    config = ScenarioConfig.from_yaml(args.config)
    bob = Bob(
        config=config,
        interface=args.interface,
        classical_host=args.host,
        classical_port=args.port,
        auth_key=resolve_auth_key(args),
        reconcile=getattr(args, "reconcile", True),
        finite_key=getattr(args, "finite_key", False),
        eps_sec=getattr(args, "eps_sec", 1e-9),
        eps_cor=getattr(args, "eps_cor", 1e-15),
        accept_timeout=getattr(args, "accept_timeout", None),
    )
    metrics = bob.run()
    if args.output:
        metrics.to_json(args.output)
        print(f"Metrics saved to {args.output}")


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    if args.role == "alice":
        _run_alice(args)
    elif args.role == "bob":
        _run_bob(args)


if __name__ == "__main__":
    main()
