#!/usr/bin/env python3
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

"""Deploy and run QFabric BB84 on FABRIC testbed.

Provisions a 3-node FABRIC slice (Alice, switch, Bob), installs BMv2,
compiles the P4 program, and runs the BB84 protocol end-to-end.

Usage:
    python scripts/deploy_fabric.py [--scenario validation/scenarios/baseline_1km.yml]
    python scripts/deploy_fabric.py --cleanup   # Delete the slice
"""

from __future__ import annotations

import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _original_print(*args, **kwargs)

import argparse
import json
import sys
import time
from pathlib import Path
import os

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from qne.config import ScenarioConfig


def get_fablib():
    """Initialize and return fablib manager."""
    from fabrictestbed_extensions.fablib.fablib import FablibManager as fablib_manager
    fablib = fablib_manager()
    fablib.show_config()
    return fablib


def create_slice(fablib, slice_name: str, site_alice: str, site_bob: str, site_switch: str):
    """Provision a 3-node FABRIC slice with L2 networks."""
    print(f"\n=== Creating slice '{slice_name}' ===")
    print(f"  Alice:  {site_alice}")
    print(f"  Switch: {site_switch}")
    print(f"  Bob:    {site_bob}")

    slice_obj = fablib.new_slice(name=slice_name)

    # Alice node
    alice = slice_obj.add_node(
        name="alice", site=site_alice, image="default_ubuntu_22",
        cores=4, ram=8, disk=20,
    )
    alice_nic = alice.add_component(
        model="NIC_Basic", name="alice_nic"
    ).get_interfaces()[0]

    # Switch node (BMv2)
    switch = slice_obj.add_node(
        name="switch", site=site_switch, image="default_ubuntu_22",
        cores=4, ram=8, disk=40,
    )
    sw_nic_a = switch.add_component(
        model="NIC_Basic", name="sw_nic_alice"
    ).get_interfaces()[0]
    sw_nic_b = switch.add_component(
        model="NIC_Basic", name="sw_nic_bob"
    ).get_interfaces()[0]

    # Bob node
    bob = slice_obj.add_node(
        name="bob", site=site_bob, image="default_ubuntu_22",
        cores=4, ram=8, disk=20,
    )
    bob_nic = bob.add_component(
        model="NIC_Basic", name="bob_nic"
    ).get_interfaces()[0]

    # L2 networks
    slice_obj.add_l2network(
        name="net_alice_switch",
        interfaces=[alice_nic, sw_nic_a],
    )
    slice_obj.add_l2network(
        name="net_switch_bob",
        interfaces=[sw_nic_b, bob_nic],
    )

    print("Submitting slice...")
    slice_obj.submit()
    print("Waiting for slice to be ready...")
    slice_obj.wait_ssh(progress=True)

    print("\n=== Slice ready ===")
    slice_obj.show()
    return slice_obj


def upload_project(slice_obj):
    """Upload the full QFabric repo to all nodes as a clean tarball.

    Ships qne + validation (+ scenarios) + p4 + scripts so each node can run both
    the data-plane experiment and the on-node cross-validation. Excludes venvs,
    VCS, and caches so the upload stays small (a raw upload of the tree can hang
    on a multi-hundred-MB .venv).
    """
    import subprocess
    import tempfile

    print("\n=== Uploading project (clean tarball) ===")
    tgz = os.path.join(tempfile.gettempdir(), "qfabric_deploy.tgz")
    subprocess.run(
        ["tar", "czf", tgz, "-C", str(PROJECT_DIR.parent),
         "--exclude=.venv", "--exclude=venv", "--exclude=.venv-*", "--exclude=.git",
         "--exclude=__pycache__", "--exclude=.pytest_cache", "--exclude=*.egg-info",
         "--exclude=dist", "--exclude=.ipynb_checkpoints", "--exclude=*.pyc",
         PROJECT_DIR.name],
        check=True,
    )
    for node_name in ["alice", "bob", "switch"]:
        node = slice_obj.get_node(node_name)
        print(f"  Uploading to {node_name}...")
        node.upload_file(tgz, "qfabric.tgz")
        # Extract OVER the existing dir (no rm): this updates the code while
        # preserving the venvs (.venv, .venv-seq, ...) that live under ~/qfabric,
        # and avoids failing on root-owned leftovers from earlier sudo runs.
        # --strip-components=1 makes the extract robust to the repo dir name.
        node.execute(
            "mkdir -p qfabric && "
            "tar xzf qfabric.tgz -C qfabric --strip-components=1 && "
            "mkdir -p qfabric/results",
            quiet=True,
        )
    print("  Upload complete (qne + validation + scenarios + p4 on every node)")


# Imported lazily inside need_imports to avoid pulling matplotlib at module load
# when only provisioning/running BB84 (not cross-validating).
def setup_sim_envs(slice_obj, netsquid_user=None, netsquid_pass=None):
    """Build the simulator environments ON the FABRIC nodes (idempotent).

    Per the chosen layout:
      * switch -> QFabric-sim  (.venv-qsim, native python3 + numpy/pyyaml)
      * alice  -> SeQUeNCe 1.0 (.venv-seq,  Python 3.12 via deadsnakes)
      * bob    -> NetSquid     (.venv-nsq,  native python3.10/3.11)

    netsquid_{user,pass} are your netsquid.org credentials (or set them in the
    NETSQUID_USER / NETSQUID_PASS environment of the caller).
    """
    netsquid_user = netsquid_user or os.environ.get("NETSQUID_USER")
    netsquid_pass = netsquid_pass or os.environ.get("NETSQUID_PASS")

    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    switch = slice_obj.get_node("switch")

    print("\n=== Setting up simulator envs on FABRIC nodes (one-time, several min) ===")

    print("  [switch] QFabric-sim (.venv-qsim)...")
    switch.execute(
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip && "
        "cd ~/qfabric && (test -d .venv-qsim || python3 -m venv .venv-qsim) && "
        ".venv-qsim/bin/pip install -q --upgrade pip && "
        ".venv-qsim/bin/pip install -q numpy pyyaml",
        quiet=False,
    )

    print("  [alice] SeQUeNCe 1.0 on Python 3.12 (.venv-seq)...")
    alice.execute(
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common && "
        "sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv && "
        "cd ~/qfabric && (test -d .venv-seq || python3.12 -m venv .venv-seq) && "
        ".venv-seq/bin/pip install -q --upgrade pip && "
        ".venv-seq/bin/pip install -q numpy pyyaml 'sequence==1.0.0'",
        quiet=False,
    )

    print("  [bob] NetSquid (.venv-nsq)...")
    creds = ""
    if netsquid_user and netsquid_pass:
        creds = f"--extra-index-url 'https://{netsquid_user}:{netsquid_pass}@pypi.netsquid.org'"
    else:
        print("    WARNING: NETSQUID_USER/NETSQUID_PASS not set — NetSquid will not install.")
    bob.execute(
        "sudo apt-get update -qq && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip && "
        "cd ~/qfabric && (test -d .venv-nsq || python3 -m venv .venv-nsq) && "
        ".venv-nsq/bin/pip install -q --upgrade pip && "
        ".venv-nsq/bin/pip install -q numpy pyyaml && "
        f".venv-nsq/bin/pip install -q {creds} netsquid",
        quiet=False,
    )
    print("  Simulator envs ready (alice=SeQUeNCe, bob=NetSquid, switch=QFabric-sim)")


def run_cross_validation_on_fabric(
    slice_obj, scenario_path="validation/scenarios/fabric_1km.yml", results_dir=None,
):
    """Run the cross-validation entirely on FABRIC nodes and return the results.

    Compares: QFabric MEASURED (the BMv2 run from run_bb84) + QFabric-sim (switch)
    + SeQUeNCe (alice) + NetSquid (bob). Requires setup_sim_envs() to have run.
    """
    from pathlib import Path as _Path
    from validation.compare import run_backend_on_node, load_fabric_result

    results_dir = _Path(results_dir) if results_dir else (PROJECT_DIR / "results")
    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    switch = slice_obj.get_node("switch")

    # Make sure every node has the matching scenario file at ~/qfabric/scenario.yml.
    for node in (alice, bob, switch):
        node.upload_file(str(PROJECT_DIR / scenario_path), "qfabric/scenario.yml")

    print("\n=== Cross-validation on FABRIC nodes ===")
    results = []

    qf = load_fabric_result(results_dir / "fabric_bob_results.json")
    if qf is not None:
        qf.platform = "qfabric"  # the measured BMv2 run
        results.append(qf)
        print(f"  QFabric (FABRIC measured): QBER={qf.qber:.4f}, sifted={qf.sifted_bits}")
    else:
        print("  (no FABRIC measurement found — run run_bb84 first to include it)")

    print("  QFabric-sim on switch...")
    results.append(run_backend_on_node(switch, ".venv-qsim/bin/python",
                                       "validation.run_qfabric", "qfabric_sim"))
    print("  SeQUeNCe on alice...")
    results.append(run_backend_on_node(alice, ".venv-seq/bin/python",
                                       "validation.run_sequence", "sequence"))
    print("  NetSquid on bob...")
    results.append(run_backend_on_node(bob, ".venv-nsq/bin/python",
                                       "validation.run_netsquid", "netsquid"))
    return results


def run_all_scenarios_on_fabric(slice_obj, scenarios_dir="validation/scenarios",
                                cross_validate=True, send_rate_hz=10000.0):
    """Run EVERY scenario in `scenarios_dir` end-to-end on the slice.

    Sweep files (those whose name contains 'sweep') are expanded into their
    individual points. For each point this reconfigures the switch loss threshold,
    runs BB84, and (optionally) the 4-way cross-validation. Results accumulate in
    results/all_scenarios.json (rewritten after each point, so partial progress
    survives an interruption). Returns the list of per-point result rows.

    Prerequisites (run once in notebooks 01/03): the slice is up with data-plane
    IPs assigned (setup_dataplane_ips) and the simulator envs built (setup_sim_envs).
    """
    import glob
    import json as _json
    import yaml
    from pathlib import Path as _Path
    from validation.scenario import ValidationScenario
    from validation.compare import load_fabric_result

    results_dir = PROJECT_DIR / "results"
    tmp_dir = results_dir / "_tmp_scenarios"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    sdir = PROJECT_DIR / scenarios_dir

    # Discover every scenario file; expand sweeps into points.
    points = []  # (ValidationScenario, group_name)
    for f in sorted(glob.glob(str(sdir / "*.yml"))):
        group = _Path(f).stem
        if "sweep" in group:
            points += [(s, group) for s in ValidationScenario.load_sweep(f)]
        else:
            points.append((ValidationScenario.from_yaml(f), group))
    print(f"Discovered scenarios in {scenarios_dir} -> {len(points)} runs")

    rows = []
    for i, (vs, group) in enumerate(points, 1):
        print(f"\n##### [{i}/{len(points)}] {group} : {vs.name} "
              f"(dist={vs.distance_km} km, atten={vs.attenuation_db_per_km} dB/km, "
              f"F={vs.polarization_fidelity}) #####")

        # Materialise a nested ScenarioConfig YAML for run_bb84 + the on-node adapters.
        cfg_dict = {
            "name": vs.name,
            "channel": {"distance_km": vs.distance_km,
                        "attenuation_db_per_km": vs.attenuation_db_per_km,
                        "polarization_fidelity": vs.polarization_fidelity},
            "detector": {"efficiency": vs.detector_efficiency,
                         "dark_count_rate": vs.dark_count_rate_hz},
            "protocol": {"num_photons": vs.num_photons,
                         "sample_fraction": vs.sample_fraction,
                         "send_rate_hz": send_rate_hz},
            "seed": vs.seed,
        }
        safe = f"{group}__{vs.name}".replace("=", "-").replace(".", "p").replace("/", "-")
        tmp_path = tmp_dir / f"{safe}.yml"
        tmp_path.write_text(yaml.safe_dump(cfg_dict))
        rel = str(tmp_path.relative_to(PROJECT_DIR))

        # Clear any prior local measurement so a failed run can't reuse it.
        for f in ("fabric_bob_results.json", "fabric_alice_results.json"):
            (results_dir / f).unlink(missing_ok=True)

        config = ScenarioConfig.from_dict(cfg_dict)
        try:
            amac, bmac, sw_a, sw_b, _, _ = configure_switch(slice_obj, config.loss_threshold_u32)
            run_bb84(slice_obj, rel, amac, bmac, sw_alice_mac=sw_a, bob_data_ip="10.10.1.2")
            if cross_validate:
                backends = run_cross_validation_on_fabric(slice_obj, rel)
            else:
                m = load_fabric_result(results_dir / "fabric_bob_results.json")
                backends = [m] if m else []
        except Exception as e:
            print(f"  !! point {group}:{vs.name} failed: {e} — recording as incomplete")
            backends = []

        # Freshness guard: reject a measured 'qfabric' point that doesn't match this
        # scenario (stale file) or produced no key, so the dataset never carries a
        # duplicated/empty measurement masquerading as real.
        for b in backends:
            if b and b.platform == "qfabric":
                if b.scenario_name != vs.name or b.sifted_bits <= 0:
                    b.extra["error"] = (f"no fresh measurement for '{vs.name}' "
                                        f"(got '{b.scenario_name}', sifted={b.sifted_bits})")

        rows.append({
            "group": group,
            "scenario": vs.name,
            "distance_km": vs.distance_km,
            "attenuation_db_per_km": vs.attenuation_db_per_km,
            "polarization_fidelity": vs.polarization_fidelity,
            "backends": [b.to_payload() for b in backends if b],
        })
        # Rewrite after every point so an interrupted sweep keeps what it has.
        (results_dir / "all_scenarios.json").write_text(_json.dumps(rows, indent=2))

    print(f"\nSaved all-scenario results -> {results_dir / 'all_scenarios.json'} "
          f"({len(rows)} points)")
    return rows


def install_deps(slice_obj):
    """Install dependencies on all nodes."""
    print("\n=== Installing dependencies ===")

    # Install Python deps on Alice and Bob. Fresh FABRIC images may ship without
    # pip/venv and with empty apt lists, so update + install those first (from the
    # 'universe' repo) before creating the venv.
    for node_name in ["alice", "bob"]:
        node = slice_obj.get_node(node_name)
        print(f"  Installing Python deps on {node_name}...")
        stdout, stderr = node.execute(
            "sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv && "
            "cd ~/qfabric && "
            "python3 -m venv .venv && "
            "source .venv/bin/activate && "
            "pip install --quiet pyyaml numpy",
            quiet=True,
        )
        # Confirm the deps actually import in the venv.
        check, _ = node.execute(
            "~/qfabric/.venv/bin/python3 -c 'import yaml, numpy; print(\"deps OK\")'",
            quiet=True,
        )
        print(f"    {node_name}: {check.strip() or stderr[:200]}")

    # Install BMv2 on switch
    switch = slice_obj.get_node("switch")
    print("  Installing BMv2 on switch (this will take a while)...")
    stdout, stderr = switch.execute(
        "cd ~/qfabric && chmod +x scripts/install_bmv2.sh && bash scripts/install_bmv2.sh",
        quiet=False,
    )
    # Verify installation succeeded
    verify_out, _ = switch.execute("which simple_switch && which p4c-bm2-ss", quiet=True)
    if "simple_switch" not in verify_out or "p4c" not in verify_out:
        print(f"  ERROR: BMv2/p4c installation failed!")
        print(f"  Verify output: {verify_out.strip()}")
        print(f"  Last stderr: {str(stderr)[-500:]}")
        raise RuntimeError("BMv2/p4c installation failed")
    print("  BMv2 installation complete")


def configure_switch(slice_obj, threshold: int):
    """Compile P4 program and start BMv2 on the switch node."""
    print(f"\n=== Configuring switch (threshold={threshold}) ===")

    switch = slice_obj.get_node("switch")

    # Get data-plane interface names
    iface_alice = switch.get_interface(network_name="net_alice_switch").get_device_name()
    iface_bob = switch.get_interface(network_name="net_switch_bob").get_device_name()
    print(f"  Switch interfaces: {iface_alice} (Alice), {iface_bob} (Bob)")

    # Get MAC addresses for L2 forwarding
    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    alice_iface = alice.get_interface(network_name="net_alice_switch")
    bob_iface = bob.get_interface(network_name="net_switch_bob")
    alice_mac = alice_iface.get_mac()
    bob_mac = bob_iface.get_mac()
    print(f"  Alice MAC: {alice_mac}")
    print(f"  Bob MAC:   {bob_mac}")

    # Switch-port MACs (used for src-MAC rewriting; FABRIC's OVS drops frames with
    # unexpected source MACs). Shared by both the source-build and Docker paths.
    alice_mac_hex = "0x" + alice_mac.replace(":", "")
    bob_mac_hex = "0x" + bob_mac.replace(":", "")
    sw_alice_mac = switch.get_interface(network_name="net_alice_switch").get_mac()
    sw_bob_mac = switch.get_interface(network_name="net_switch_bob").get_mac()
    sw_alice_mac_hex = "0x" + sw_alice_mac.replace(":", "")
    sw_bob_mac_hex = "0x" + sw_bob_mac.replace(":", "")
    print(f"  Switch Alice-side MAC: {sw_alice_mac}")
    print(f"  Switch Bob-side MAC:   {sw_bob_mac}")

    home_out, _ = switch.execute("echo $HOME", quiet=True)
    home = home_out.strip()
    json_rel = "p4/bmv2/quantum_channel.json"
    image = os.environ.get("QFABRIC_BMV2_IMAGE", "").strip()

    if image:
        # ---- Prebuilt Docker image: no per-deploy source build ----
        # Set up the switch with `scripts/setup_switch_docker.sh` (installs Docker
        # + pulls the image) first; export QFABRIC_BMV2_IMAGE to enable this path.
        print(f"  Using BMv2 Docker image: {image}")
        print("  Compiling P4 (in container)...")
        switch.execute(
            f"sudo docker run --rm -v {home}/qfabric:/work {image} "
            f"p4c-bm2-ss --std p4-16 -o /work/{json_rel} "
            f"-I /work/p4/bmv2/includes /work/p4/bmv2/quantum_channel.p4",
            quiet=True,
        )
        print("  Starting BMv2 (container: --privileged --network host)...")
        switch.execute(
            "sudo docker rm -f bmv2 2>/dev/null; "
            f"sudo docker run -d --name bmv2 --privileged --network host "
            f"-v {home}/qfabric:/work {image} "
            f"simple_switch --interface 0@{iface_alice} --interface 1@{iface_bob} "
            f"--log-level warn /work/{json_rel}",
            quiet=True,
        )
        time.sleep(4)
        ps_out, _ = switch.execute(
            "sudo docker exec bmv2 pgrep -a simple_switch 2>/dev/null || true", quiet=True)
        if "simple_switch" not in ps_out:
            log_out, _ = switch.execute("sudo docker logs --tail 20 bmv2 2>&1 || true", quiet=True)
            print(f"  ERROR: BMv2 container not running!\n  Logs: {log_out.strip()}")
            raise RuntimeError("BMv2 (docker) failed to start")
        cli = "sudo docker exec -i bmv2 simple_switch_CLI"
    else:
        # ---- Build-from-source path (p4c-bm2-ss + systemd-run) ----
        print("  Compiling P4 program...")
        switch.execute(
            "cd ~/qfabric && export PATH=/usr/local/bin:$PATH && "
            f"p4c-bm2-ss --std p4-16 -o {json_rel} -I p4/bmv2/includes "
            "p4/bmv2/quantum_channel.p4",
            quiet=True,
        )
        print("  Starting BMv2...")
        switch.execute(
            "sudo systemctl stop bmv2 2>/dev/null; "
            "sudo systemctl reset-failed bmv2 2>/dev/null; "
            "sudo pkill -f simple_switch 2>/dev/null; sleep 2",
            quiet=True,
        )
        ss_path_out, _ = switch.execute(
            "which simple_switch 2>/dev/null || "
            "find /usr/local/bin /usr/bin -name simple_switch 2>/dev/null | head -1", quiet=True)
        ss_path = ss_path_out.strip() or "/usr/local/bin/simple_switch"
        switch.execute(
            f"sudo systemd-run --unit=bmv2 --remain-after-exit {ss_path} "
            f"--interface 0@{iface_alice} --interface 1@{iface_bob} "
            f"--log-level warn {home}/qfabric/{json_rel}",
            quiet=True,
        )
        time.sleep(3)
        ps_out, _ = switch.execute("pgrep -a simple_switch", quiet=True)
        if not ps_out.strip():
            log_out, _ = switch.execute("sudo journalctl -u bmv2 --no-pager -n 20 2>/dev/null", quiet=True)
            print(f"  ERROR: BMv2 failed to start!\n  Journal: {log_out.strip()}")
            raise RuntimeError("BMv2 failed to start")
        cli = "/usr/local/bin/simple_switch_CLI"

    # ---- Configure tables (shared by both paths) ----
    print("  Configuring tables...")
    for cmd in (
        f"table_add quantum_channel_params set_channel_params 0 => {threshold} 1 {sw_bob_mac_hex} {bob_mac_hex}",
        f"table_add port_forwarding port_forward 0 => 1 {sw_bob_mac_hex} {bob_mac_hex}",
        f"table_add port_forwarding port_forward 1 => 0 {sw_alice_mac_hex} {alice_mac_hex}",
    ):
        switch.execute(f'echo "{cmd}" | {cli} --thrift-port 9090', quiet=True)

    print("  Switch configured and running")
    return alice_mac, bob_mac, sw_alice_mac, sw_bob_mac, iface_alice, iface_bob


def setup_dataplane_ips(slice_obj, alice_mac: str, bob_mac: str):
    """Assign data-plane IPs and static ARP entries on Alice and Bob.

    This routes the classical BB84 sifting channel over the L2 data-plane
    network instead of the FABRIC management network (which blocks arbitrary
    TCP ports between sites).
    """
    print("\n=== Setting up data-plane IPs ===")

    alice_node = slice_obj.get_node("alice")
    bob_node = slice_obj.get_node("bob")

    alice_iface = alice_node.get_interface(network_name="net_alice_switch").get_device_name()
    bob_iface = bob_node.get_interface(network_name="net_switch_bob").get_device_name()

    alice_ip = "10.10.1.1"
    bob_ip = "10.10.1.2"

    # Assign IPs
    print(f"  Alice: {alice_ip}/24 on {alice_iface}")
    alice_node.execute(
        f"sudo ip addr flush dev {alice_iface} && "
        f"sudo ip addr add {alice_ip}/24 dev {alice_iface} && "
        f"sudo ip link set {alice_iface} up",
        quiet=True,
    )

    print(f"  Bob:   {bob_ip}/24 on {bob_iface}")
    bob_node.execute(
        f"sudo ip addr flush dev {bob_iface} && "
        f"sudo ip addr add {bob_ip}/24 dev {bob_iface} && "
        f"sudo ip link set {bob_iface} up",
        quiet=True,
    )

    # Static ARP entries using ip neigh (Rocky 9 compatible)
    # Use the SWITCH port MACs as the neighbor addresses because:
    # 1. BMv2 rewrites src_mac → endpoint MACs are never learned on the
    #    FABRIC L2 segments → OVS drops frames with unknown dst MACs
    # 2. Using switch port MACs means dst_mac in frames always matches
    #    a known MAC on the FABRIC L2 segment
    print("  Adding static ARP entries...")
    switch = slice_obj.get_node("switch")
    sw_alice_mac = switch.get_interface(network_name="net_alice_switch").get_mac().lower()
    sw_bob_mac = switch.get_interface(network_name="net_switch_bob").get_mac().lower()
    print(f"  ARP: Alice → {bob_ip} via {sw_alice_mac} (switch Alice-side)")
    print(f"  ARP: Bob → {alice_ip} via {sw_bob_mac} (switch Bob-side)")

    # Alice sends to switch's Alice-side MAC (which is on net_alice_switch)
    alice_node.execute(
        f"sudo ip neigh replace {bob_ip} lladdr {sw_alice_mac} nud permanent dev {alice_iface}",
        quiet=True,
    )
    # Bob sends to switch's Bob-side MAC (which is on net_switch_bob)
    bob_node.execute(
        f"sudo ip neigh replace {alice_ip} lladdr {sw_bob_mac} nud permanent dev {bob_iface}",
        quiet=True,
    )

    # Promiscuous mode for raw sockets
    alice_node.execute(f"sudo ip link set {alice_iface} promisc on", quiet=True)
    bob_node.execute(f"sudo ip link set {bob_iface} promisc on", quiet=True)

    # Verify connectivity
    print("  Testing connectivity (ping)...")
    stdout, stderr = alice_node.execute(
        f"ping -c 3 -W 2 {bob_ip}",
        quiet=True,
    )
    if "0 received" in stdout or "100% packet loss" in stdout:
        print(f"  WARNING: Ping failed! Output: {stdout.strip()}")
        # Debug: check BMv2 is running
        switch = slice_obj.get_node("switch")
        ps_out, _ = switch.execute("pgrep -a simple_switch", quiet=True)
        print(f"  Switch process: {ps_out.strip()}")
        table_out, _ = switch.execute(
            'echo "table_dump port_forwarding" | /usr/local/bin/simple_switch_CLI --thrift-port 9090 2>/dev/null',
            quiet=True,
        )
        print(f"  Port forwarding table: {table_out.strip()}")
    else:
        print(f"  Ping successful!")

    return alice_ip, bob_ip


def run_bb84(slice_obj, scenario_path: str, alice_mac: str, bob_mac: str,
             sw_alice_mac: str = None, bob_data_ip: str = None):
    """Run BB84 protocol: start Bob, then Alice."""
    print("\n=== Running BB84 protocol ===")

    alice_node = slice_obj.get_node("alice")
    bob_node = slice_obj.get_node("bob")

    # Upload scenario config
    for node in [alice_node, bob_node]:
        node.upload_file(str(PROJECT_DIR / scenario_path), "qfabric/scenario.yml")

    # Get interface names
    alice_iface = alice_node.get_interface(network_name="net_alice_switch").get_device_name()
    bob_iface = bob_node.get_interface(network_name="net_switch_bob").get_device_name()

    # Use data-plane IP for classical channel (management network blocks TCP)
    if bob_data_ip:
        bob_classical_ip = bob_data_ip
        print(f"  Bob data-plane IP: {bob_classical_ip} (for classical channel)")
    else:
        bob_classical_ip = bob_node.get_management_ip()
        print(f"  Bob management IP: {bob_classical_ip} (for classical channel)")
    print(f"  Alice data iface:  {alice_iface}")
    print(f"  Bob data iface:    {bob_iface}")

    # Kill any leftover processes and remove old results. Use sudo rm because the
    # result files are written by the sudo-run qne.cli (root-owned), so a plain rm
    # would fail and a failed run could then reuse the previous point's stale result.
    print("  Cleaning up previous runs...")
    bob_node.execute(
        "sudo pkill -f 'qne.cli' 2>/dev/null; "
        "sudo rm -f ~/qfabric/results/*.json /tmp/bob.log; sleep 1",
        quiet=True,
    )
    alice_node.execute(
        "sudo pkill -f 'qne.cli' 2>/dev/null; "
        "sudo rm -f ~/qfabric/results/*.json /tmp/alice.log; sleep 1",
        quiet=True,
    )

    # Ensure venv + deps exist on Alice and Bob
    for node_name, node in [("alice", alice_node), ("bob", bob_node)]:
        print(f"  Ensuring deps on {node_name}...")
        node.execute(
            "cd ~/qfabric && "
            "(test -d .venv || python3 -m venv .venv) && "
            "source .venv/bin/activate && "
            "pip install --quiet pyyaml numpy 2>/dev/null",
            quiet=True,
        )

    # Start Bob (receiver) in background using execute_thread
    print("  Starting Bob...")
    bob_thread = bob_node.execute_thread(
        f"cd ~/qfabric && "
        f"sudo -E ~/qfabric/.venv/bin/python3 -m qne.cli bob "
        f"--config scenario.yml "
        f"--interface {bob_iface} "
        f"--host '0.0.0.0' "
        f"--output results/bob_results.json "
        f"2>&1 | tee /tmp/bob.log",
    )
    time.sleep(10)  # Allow Bob time to start SSH + Python + open raw socket

    # Build Alice MAC args: dst_mac = switch's Alice-side MAC (for FABRIC OVS delivery)
    # src_mac = Alice's own MAC (known on net_alice_switch segment)
    mac_args = ""
    if sw_alice_mac:
        mac_args += f" --dst-mac '{sw_alice_mac}' --src-mac '{alice_mac}'"

    # Run Alice (sender) in background using execute_thread
    print(f"  Alice MAC args: {mac_args}")
    print("  Starting Alice...")
    alice_thread = alice_node.execute_thread(
        f"cd ~/qfabric && "
        f"sudo -E ~/qfabric/.venv/bin/python3 -m qne.cli alice "
        f"--config scenario.yml "
        f"--interface {alice_iface} "
        f"--bob-host '{bob_classical_ip}' "
        f"{mac_args} "
        f"--output results/alice_results.json "
        f"2>&1 | tee /tmp/alice.log",
    )

    # Wait for Alice thread to complete (Bob should also finish)
    print("  Waiting for BB84 to complete...")
    alice_result = alice_thread.result()
    print(f"  Alice finished")
    print(f"  Alice output: {str(alice_result[0])[:2000]}")
    if alice_result[1]:
        print(f"  Alice stderr: {str(alice_result[1])[:300]}")

    # Give Bob a few more seconds, then join
    time.sleep(5)
    try:
        bob_result = bob_thread.result()
        print(f"  Bob finished")
        print(f"  Bob output: {str(bob_result[0])[:500]}")
    except Exception as e:
        print(f"  Bob thread: {e}")

    # Collect results
    print("\n=== Collecting results ===")
    results_dir = PROJECT_DIR / "results"
    results_dir.mkdir(exist_ok=True)

    for node, role in [(bob_node, "bob"), (alice_node, "alice")]:
        try:
            stdout, _ = node.execute(
                f"cat ~/qfabric/results/{role}_results.json",
                quiet=True,
            )
            local_path = results_dir / f"fabric_{role}_results.json"
            local_path.write_text(stdout)
            print(f"  Saved {role} results to {local_path}")
        except Exception as e:
            print(f"  Error fetching {role} results: {e}")

    # Display summary from Bob results (tolerate an empty/failed run — e.g. at high
    # loss where no key forms — so a sweep doesn't abort on one bad point).
    bob_path = results_dir / "fabric_bob_results.json"
    bob_text = bob_path.read_text().strip() if bob_path.exists() else ""
    if bob_text:
        try:
            bob_results = json.loads(bob_text)
            print("\n=== FABRIC BB84 Results ===")
            for key in ["photons_sent", "photons_received", "sifted_bits",
                         "qber", "secure_key_rate", "final_key_bits", "elapsed_seconds"]:
                print(f"  {key}: {bob_results.get(key, 'N/A')}")
        except json.JSONDecodeError:
            print("\n  WARNING: Bob results file is not valid JSON — run likely failed.")
    else:
        print("\n  WARNING: no Bob results — the BB84 run produced no output "
              "(possible at high loss, or the run failed). This point will be "
              "reported as missing, not stale.")


def cleanup(fablib, slice_name: str):
    """Delete the FABRIC slice."""
    print(f"\n=== Deleting slice '{slice_name}' ===")
    try:
        slice_obj = fablib.get_slice(name=slice_name)
        slice_obj.delete()
        print("  Slice deleted")
    except Exception as e:
        print(f"  Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Deploy QFabric BB84 on FABRIC")
    parser.add_argument(
        "--scenario", default="validation/scenarios/baseline_1km.yml",
        help="Scenario YAML config file",
    )
    parser.add_argument("--slice-name", default="qfabric-bb84", help="FABRIC slice name")
    parser.add_argument("--site-alice", default="TACC", help="FABRIC site for Alice")
    parser.add_argument("--site-bob", default="STAR", help="FABRIC site for Bob")
    parser.add_argument("--site-switch", default="TACC", help="FABRIC site for BMv2 switch")
    parser.add_argument("--cleanup", action="store_true", help="Delete the slice and exit")
    parser.add_argument("--skip-provision", action="store_true",
                        help="Skip provisioning (use existing slice)")
    parser.add_argument("--skip-install", action="store_true",
                        help="Skip BMv2 installation (already installed)")
    args = parser.parse_args()

    fablib = get_fablib()

    if args.cleanup:
        cleanup(fablib, args.slice_name)
        return

    # Load scenario config for threshold computation
    config = ScenarioConfig.from_yaml(PROJECT_DIR / args.scenario)
    threshold = config.loss_threshold_u32
    print(f"\nScenario: {config.name}")
    print(f"  Distance: {config.channel.distance_km} km")
    print(f"  Loss probability: {config.loss_probability:.4f}")
    print(f"  P4 threshold: {threshold}")

    # Provision or reuse slice
    if args.skip_provision:
        print(f"\n=== Using existing slice '{args.slice_name}' ===")
        slice_obj = fablib.get_slice(name=args.slice_name)
        slice_obj.show()
    else:
        slice_obj = create_slice(
            fablib, args.slice_name,
            args.site_alice, args.site_bob, args.site_switch,
        )

    # Upload project
    upload_project(slice_obj)

    # Install dependencies
    if not args.skip_install:
        install_deps(slice_obj)

    # Configure and start switch
    alice_mac, bob_mac, sw_alice_mac, sw_bob_mac, _, _ = configure_switch(slice_obj, threshold)

    # Set up data-plane IPs for classical channel
    alice_ip, bob_ip = setup_dataplane_ips(slice_obj, alice_mac, bob_mac)

    # Run BB84 (using data-plane IP for classical channel)
    run_bb84(slice_obj, args.scenario, alice_mac, bob_mac,
             sw_alice_mac=sw_alice_mac, bob_data_ip=bob_ip)

    print("\n=== Done ===")
    print(f"Slice '{args.slice_name}' is still active.")
    print(f"To clean up: python scripts/deploy_fabric.py --cleanup")


if __name__ == "__main__":
    main()
