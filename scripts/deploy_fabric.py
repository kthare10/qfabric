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


def loss_probability(distance_km: float, attenuation_db_per_km: float) -> float:
    """Fiber loss as a per-photon drop probability: P = 1 - 10^(-alpha*L/10).

    Used to size the P4 loss threshold per distance and to report analytical loss.
    """
    return 1.0 - 10 ** (-(attenuation_db_per_km * distance_km) / 10.0)


def get_fablib():
    """Initialize and return fablib manager."""
    from fabrictestbed_extensions.fablib.fablib import FablibManager as fablib_manager
    fablib = fablib_manager()
    fablib.show_config()
    return fablib


def create_slice(fablib, slice_name: str, site_alice: str, site_bob: str, site_switch: str):
    """Provision a 3-node FABRIC slice with L2 networks.

    Default topology is SINGLE-SITE (all three nodes on one site): a photon
    cannot physically cross a WAN-scale span, so the quantum link must live on
    site-local L2 (sub-ms, ~the "near-zero classical network" of real QKD) and
    distance is EMULATED — the P4 loss threshold and `--channel-delay auto`
    both derive from the same distance_km knob. Pass a different site for Bob
    only for classical-plane stress experiments (explicitly not a realistic
    quantum-channel topology; 2026-07-15 review).
    """
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
    if netsquid_user in (None, "", "...") or netsquid_pass in (None, "", "..."):
        print("    WARNING: NETSQUID_USER/NETSQUID_PASS not set (or left as the "
              "'...' placeholder) — NetSquid will not install. Set real "
              "credentials in the environment and re-run.")
    else:
        # Percent-encode: the password may contain characters that break either
        # the URL netloc (@ : / #) or the remote shell ($ ` ' ! space). Encoding
        # both to %XX makes the token safe in the command AND correct as URL
        # userinfo (pip decodes it back before authenticating).
        from urllib.parse import quote
        enc_user = quote(netsquid_user, safe="")
        enc_pass = quote(netsquid_pass, safe="")
        creds = (f"--extra-index-url "
                 f"'https://{enc_user}:{enc_pass}@pypi.netsquid.org'")
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


def setup_switch_docker(slice_obj, image="ghcr.io/kthare10/qfabric-bmv2:latest"):
    """Install Docker on the switch and pull the prebuilt BMv2 image.

    Use this instead of the switch's source build (install_deps build_bmv2=False),
    then export QFABRIC_BMV2_IMAGE=<image> so configure_switch runs simple_switch
    from the container. Returns the image ref.
    """
    print(f"\n=== Preparing switch to run BMv2 from Docker image: {image} ===")
    switch = slice_obj.get_node("switch")
    switch.execute(
        f"cd ~/qfabric && chmod +x scripts/setup_switch_docker.sh && "
        f"bash scripts/setup_switch_docker.sh '{image}'",
        quiet=False,
    )
    return image


def install_deps(slice_obj, build_bmv2=True):
    """Install dependencies on all nodes.

    Alice/Bob always get the Python runtime deps. The switch's BMv2/p4c source
    build (slow) is skipped when build_bmv2=False — use that with
    setup_switch_docker() to run BMv2 from a prebuilt container instead.
    """
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

    if not build_bmv2:
        print("  Skipping switch BMv2 source build (using prebuilt Docker image).")
        return

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
        print("  ERROR: BMv2/p4c installation failed!")
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
        # Emulated classical channel (0x7102): same bidirectional 0<->1 pipe +
        # MAC rewrite as port_forwarding, but through the dedicated classical
        # table/counter so classical frames are classified and counted (no loss).
        f"table_add classical_channel_params classical_forward 0 => 1 {sw_bob_mac_hex} {bob_mac_hex}",
        f"table_add classical_channel_params classical_forward 1 => 0 {sw_alice_mac_hex} {alice_mac_hex}",
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
        print("  Ping successful!")

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
    print("  Alice finished")
    print(f"  Alice output: {str(alice_result[0])[:2000]}")
    if alice_result[1]:
        print(f"  Alice stderr: {str(alice_result[1])[:300]}")

    # Give Bob a few more seconds, then join
    time.sleep(5)
    try:
        bob_result = bob_thread.result()
        print("  Bob finished")
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


_CLASSICAL_PORT = 5100


def _netem_spec(delay_ms, jitter_ms, loss_pct):
    netem = "netem"
    if delay_ms:
        netem += f" delay {delay_ms}ms" + (f" {jitter_ms}ms" if jitter_ms else "")
    if loss_pct:
        netem += f" loss {loss_pct}%"
    return netem


def _install_netem(node, iface, netem, protocols):
    """prio qdisc + netem band on `iface`, steering the given ethertypes into it.

    `protocols` are tc `protocol` selectors (e.g. "ip", "0x7102"); a match-all u32
    (mask 0) sends every frame of that ethertype through the netem band. Other
    traffic falls through the default bands unaffected.
    """
    cmd = (f"sudo tc qdisc del dev {iface} root 2>/dev/null; "
           f"sudo tc qdisc add dev {iface} root handle 1: prio && "
           f"sudo tc qdisc add dev {iface} parent 1:3 handle 30: {netem}")
    for proto in protocols:
        cmd += (f" && sudo tc filter add dev {iface} parent 1:0 protocol {proto} "
                f"prio 1 u32 match u32 0 0 flowid 1:3")
    node.execute(cmd, quiet=True)


def apply_classical_netem(slice_obj, delay_ms=0, jitter_ms=0, loss_pct=0.0,
                          alice_delay_ms=None, bob_delay_ms=None,
                          classical_transport="tcp", impair_quantum=False):
    """Impair the classical channel as a **stress test** — NOT a realistic operating
    point. Real QKD's classical channel is engineered ~0 delay/loss; the realistic
    delay is the model-layer `--channel-delay auto` (4.9 us/km of the same L),
    delivered at exact sim times by the lookahead scheduler. Use netem only for
    jitter/congestion/loss sensitivity studies.

    Two mechanisms, selected by `classical_transport`:

    * ``"tcp"`` (legacy): netem on each ENDPOINT data-plane iface, u32-filtered on
      the classical sifting port (TCP:5100) so photon 0x7101 frames (non-IP) fall
      through untouched.
    * ``"l2"``: netem on the SWITCH's two egress ports, matched by **EtherType
      0x7102** — the L2 classical frame is non-IP, so the old TCP:5100 filter would
      silently no-op. This is the honest split: the P4 pipeline owns classify /
      forward / loss, and netem on the switch egress owns propagation delay (BMv2
      has no primitive to hold a packet). `impair_quantum=True` also delays the
      0x7101 photons on the Bob-side egress (models photon flight time).

    delay_ms/jitter_ms/loss_pct apply symmetrically; pass alice_delay_ms /
    bob_delay_ms for asymmetric per-direction latency (toward Alice / toward Bob).

    NOTE (slice): libpcap's default PACKET_QDISC_BYPASS can make BMv2's TX skip the
    egress qdisc on some builds; if switch-egress netem shows no effect, disable the
    bypass (or prefer the model-layer `--channel-delay` for fidelity). Verify live.
    """
    a_delay = delay_ms if alice_delay_ms is None else alice_delay_ms
    b_delay = delay_ms if bob_delay_ms is None else bob_delay_ms

    if classical_transport == "l2":
        # Switch egress: toward Alice (B->A classical) and toward Bob (A->B
        # classical + photons). Match 0x7102 both ways; add 0x7101 only on the
        # Bob-side egress (the photon direction) when impair_quantum is set.
        switch = slice_obj.get_node("switch")
        print("\n=== Applying classical-channel netem (SWITCH egress, EtherType 0x7102"
              f"{' + 0x7101' if impair_quantum else ''}) [STRESS] ===")
        legs = [
            ("toward-alice", "net_alice_switch", a_delay, ["0x7102"]),
            ("toward-bob", "net_switch_bob", b_delay,
             ["0x7102"] + (["0x7101"] if impair_quantum else [])),
        ]
        for label, netname, d, protos in legs:
            iface = switch.get_interface(network_name=netname).get_device_name()
            netem = _netem_spec(d, jitter_ms, loss_pct)
            _install_netem(switch, iface, netem, protos)
            print(f"  switch {label} ({iface}): {netem} [{', '.join(protos)}]")
        return

    # Legacy TCP path: netem on the endpoints, filtered on the classical port.
    print(f"\n=== Applying classical-channel netem (endpoints, TCP:{_CLASSICAL_PORT}) "
          "[STRESS] ===")
    for name, netname, d in (("alice", "net_alice_switch", a_delay),
                             ("bob", "net_switch_bob", b_delay)):
        node = slice_obj.get_node(name)
        iface = node.get_interface(network_name=netname).get_device_name()
        netem = _netem_spec(d, jitter_ms, loss_pct)
        node.execute(
            f"sudo tc qdisc del dev {iface} root 2>/dev/null; "
            f"sudo tc qdisc add dev {iface} root handle 1: prio && "
            f"sudo tc qdisc add dev {iface} parent 1:3 handle 30: {netem} && "
            f"sudo tc filter add dev {iface} parent 1:0 protocol ip prio 1 u32 "
            f"match ip dport {_CLASSICAL_PORT} 0xffff flowid 1:3 && "
            f"sudo tc filter add dev {iface} parent 1:0 protocol ip prio 1 u32 "
            f"match ip sport {_CLASSICAL_PORT} 0xffff flowid 1:3",
            quiet=True,
        )
        print(f"  {name} ({iface}): {netem}")


def clear_classical_netem(slice_obj):
    """Remove any netem/tc qdisc from BOTH endpoints and the switch egress ports
    (covers both the legacy TCP and the L2 classical-netem placements)."""
    targets = [("alice", "net_alice_switch"), ("bob", "net_switch_bob"),
               ("switch", "net_alice_switch"), ("switch", "net_switch_bob")]
    for name, netname in targets:
        try:
            node = slice_obj.get_node(name)
            iface = node.get_interface(network_name=netname).get_device_name()
            node.execute(f"sudo tc qdisc del dev {iface} root 2>/dev/null || true", quiet=True)
        except Exception:  # noqa: BLE001 — best-effort cleanup across placements
            pass
    print("  cleared classical netem on Alice, Bob, and the switch")


def run_network_conditions_experiment(
    slice_obj, scenario_path="validation/scenarios/fabric_1km.yml", conditions=None,
):
    """Sweep CLASSICAL-channel **stress** conditions and measure their effect on BB84.

    These are sensitivity/stress studies, NOT realistic operating points: real QKD's
    classical channel is engineered ~0 delay/loss. The *realistic* headline number is
    the `baseline` condition run with the model-layer delay (`channel_delay="auto"` →
    4.9 us/km of the same L), which the lookahead scheduler delivers at exact sim
    times; the delay/jitter/loss conditions below are the stress envelope around it.

    For each condition, impair the classical channel, run BB84, and record QBER
    (should be ~flat — the channel is reliable), elapsed time-to-key, and effective
    key bits/second. This isolates the real-network impact that ideal-channel
    simulators (SeQUeNCe/NetSquid) cannot capture. Results → results/network_effects.json.

    Prereqs: configure_switch + setup_dataplane_ips already done (notebook 01).
    `conditions` is a list of dicts: {name, delay_ms, jitter_ms, loss_pct,
    alice_delay_ms, bob_delay_ms, classical_transport}; defaults to a representative
    set. Pass `classical_transport="l2"` in a condition to stress the raw-0x7102
    path on the switch egress instead of the TCP endpoints.
    """
    import json as _json

    if conditions is None:
        conditions = [
            {"name": "baseline"},
            {"name": "latency_25ms", "delay_ms": 25},
            {"name": "latency_100ms", "delay_ms": 100},
            {"name": "jitter_50pm20ms", "delay_ms": 50, "jitter_ms": 20},
            {"name": "loss_1pct", "loss_pct": 1.0},
            {"name": "asymmetric_100_10ms", "alice_delay_ms": 100, "bob_delay_ms": 10},
        ]

    results_dir = PROJECT_DIR / "results"
    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    switch = slice_obj.get_node("switch")
    alice_mac = alice.get_interface(network_name="net_alice_switch").get_mac()
    bob_mac = bob.get_interface(network_name="net_switch_bob").get_mac()
    sw_alice_mac = switch.get_interface(network_name="net_alice_switch").get_mac()

    rows = []
    try:
        for i, cond in enumerate(conditions, 1):
            name = cond.get("name", f"cond{i}")
            print(f"\n##### [{i}/{len(conditions)}] classical condition: {name} #####")
            clear_classical_netem(slice_obj)
            netem_kw = {k: v for k, v in cond.items() if k != "name"}
            if netem_kw:
                apply_classical_netem(slice_obj, **netem_kw)

            (results_dir / "fabric_bob_results.json").unlink(missing_ok=True)
            try:
                run_bb84(slice_obj, scenario_path, alice_mac, bob_mac,
                         sw_alice_mac=sw_alice_mac, bob_data_ip="10.10.1.2")
            except Exception as e:
                print(f"  !! run failed under {name}: {e}")

            row = {"condition": name, **netem_kw}
            bob_path = results_dir / "fabric_bob_results.json"
            txt = bob_path.read_text().strip() if bob_path.exists() else ""
            if txt:
                try:
                    d = _json.loads(txt)
                    elapsed = d.get("elapsed_seconds", 0.0) or 0.0
                    fk = d.get("final_key_bits", 0)
                    row.update({
                        "qber": d.get("qber", 0.0),
                        "sifted_bits": d.get("sifted_bits", 0),
                        "final_key_bits": fk,
                        "secure_key_rate": d.get("secure_key_rate", 0.0),  # bits/photon
                        "elapsed_seconds": elapsed,
                        "key_bits_per_sec": (fk / elapsed) if elapsed > 0 else 0.0,
                    })
                except _json.JSONDecodeError:
                    row["error"] = "invalid results json"
            else:
                row["error"] = "no result (run failed/timed out under this condition)"
            print(f"  {name}: QBER={row.get('qber')}, elapsed={row.get('elapsed_seconds')}s, "
                  f"bits/s={row.get('key_bits_per_sec')}")
            rows.append(row)
            (results_dir / "network_effects.json").write_text(_json.dumps(rows, indent=2))
    finally:
        clear_classical_netem(slice_obj)

    print(f"\nSaved -> {results_dir / 'network_effects.json'} ({len(rows)} conditions)")
    return rows


def setup_sequence_runtime(slice_obj, venv=".venv-qne", nodes=("alice", "bob")):
    """Build the distributed-SeQUeNCe emulator runtime on the given nodes.

    `qne_sequence.node_runner` needs `sequence==1.0.0` (Python 3.12) + numpy, plus
    qfabric's own `qne` package (reached via PYTHONPATH=~/qfabric). This installs a
    dedicated `.venv-qne` on each node and verifies the full import chain
    (sequence + qne + qne_sequence). Idempotent. Run once per slice (after
    upload_project so qne-sequence/ is present on the nodes).

    Default nodes are the two endpoints; pass ("alice", "bob", "switch") when the
    switch node will host the repeater station (run_sequence_repeater).
    """
    print(f"\n=== Setting up SeQUeNCe-emulator runtime ({venv}) on {'+'.join(nodes)} ===")
    for name in nodes:
        node = slice_obj.get_node(name)
        print(f"  [{name}] python3.12 venv + sequence==1.0.0 ...")
        node.execute(
            "sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common && "
            "sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv && "
            f"cd ~/qfabric && (test -d {venv} || python3.12 -m venv {venv}) && "
            f"{venv}/bin/pip install -q --upgrade pip && "
            f"{venv}/bin/pip install -q numpy pyyaml 'sequence==1.0.0'",
            quiet=False,
        )
        check, _ = node.execute(
            f"cd ~/qfabric/qne-sequence && PYTHONPATH=$HOME/qfabric "
            f"$HOME/qfabric/{venv}/bin/python -m qne_sequence.node_runner --help "
            f">/dev/null 2>&1 && echo 'import chain OK' || echo 'IMPORT FAILED'",
            quiet=True,
        )
        print(f"    {name}: {check.strip()}")
    print(f"  SeQUeNCe-emulator runtime ready on {'+'.join(nodes)}")


def setup_repeater_bridge(slice_obj, station_ip="10.10.1.3"):
    """Give the switch node a data-plane presence — the repeater STATION.

    The repeater protocol has no photon plane, so BMv2 isn't needed; what the
    middle node needs instead is a TCP endpoint on the 10.10.1.0/24 data plane
    (the FABRIC management network blocks arbitrary TCP). This:

      1. stops BMv2 — the `bmv2` Docker container (mutually exclusive with the
         bridge — both would forward, and BMv2 holds the ports),
      2. joins the switch's two data-plane interfaces in a Linux bridge
         (`br-qne`) carrying ``station_ip``, with ip_forward on so alice<->bob
         traffic still crosses the middle node (now via the kernel instead of
         BMv2),
      3. installs static ARP on all three nodes (the same FABRIC/OVS MAC-learning
         workaround as setup_dataplane_ips: endpoints address the switch-port
         MACs, which are always known on their L2 segment).

    Four things make it actually work — all learned the hard way on the first
    live run (validated end-to-end: alice & bob extract an identical key):
      * The `bridge` kernel module is `modprobe`d first. On a fresh switch VM it
        isn't loaded, so `ip link add type bridge` silently fails and br-qne is
        never created (the ports get flushed but never enslaved).
      * ``br-qne`` is given the ALICE-side port MAC (``sw_a_mac``). The station IP
        lives on the bridge, and the endpoints address switch-port MACs; if the
        bridge kept its own auto-assigned MAC, nothing on Alice's segment would
        answer ``station_ip`` and ``alice -> station`` fails (the run then hangs
        at Alice's first connect). Enslaved-port MACs are local FDB entries, so
        Bob's segment still reaches the bridge via ``sw_b_mac``.
      * The kernel FORWARD path is explicitly opened (``iptables -P FORWARD
        ACCEPT`` + ``rp_filter=0``). BMv2's Docker install typically leaves the
        FORWARD chain on DROP, which silently kills the routed alice<->bob tail.
      * ICMP redirects are disabled (``send_redirects=0`` on the switch,
        ``accept_redirects=0`` on the endpoints). alice<->bob is routed back out
        the same interface, so without this the switch spams redirects and the
        path drops ~50% of packets.

    Prereq: setup_dataplane_ips already ran (alice/bob hold 10.10.1.1/.2).
    Reversible: re-run notebook 02 / configure_switch to restore BMv2.
    Raises RuntimeError if connectivity can't be established (so the caller never
    launches the 3-process run into a hang).
    """
    print(f"\n=== Setting up repeater station bridge (station {station_ip}) ===")
    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    switch = slice_obj.get_node("switch")

    if_a = switch.get_interface(network_name="net_alice_switch").get_device_name()
    if_b = switch.get_interface(network_name="net_switch_bob").get_device_name()
    sw_a_mac = switch.get_interface(network_name="net_alice_switch").get_mac().lower()
    sw_b_mac = switch.get_interface(network_name="net_switch_bob").get_mac().lower()
    alice_iface = alice.get_interface(network_name="net_alice_switch").get_device_name()
    bob_iface = bob.get_interface(network_name="net_switch_bob").get_device_name()
    alice_mac = alice.get_interface(network_name="net_alice_switch").get_mac().lower()
    bob_mac = bob.get_interface(network_name="net_switch_bob").get_mac().lower()

    print(f"  switch: br-qne over {if_a} + {if_b} (MAC {sw_a_mac}), "
          "ip_forward + FORWARD ACCEPT (BMv2 stopped)")
    switch.execute(
        # BMv2 runs as the `bmv2` Docker container (--network host, see
        # configure_switch); `pkill simple_switch` on the host CANNOT stop it and
        # it keeps L2-forwarding + holding the ports, which blocks the bridge.
        "sudo docker rm -f bmv2 >/dev/null 2>&1 || true; "
        "sudo pkill -f simple_switch >/dev/null 2>&1 || true; sleep 1; "
        # a fresh switch VM doesn't have the bridge module loaded, so
        # `ip link add type bridge` silently fails — load it first.
        "sudo modprobe bridge || true; "
        "sudo ip link del br-qne 2>/dev/null || true; "
        "sudo ip link add name br-qne type bridge; "
        f"sudo ip addr flush dev {if_a}; sudo ip addr flush dev {if_b}; "
        f"sudo ip link set {if_a} master br-qne; sudo ip link set {if_b} master br-qne; "
        f"sudo ip link set {if_a} up; sudo ip link set {if_b} up; "
        # bridge takes the alice-side port MAC so station_ip is answerable there
        "sudo ip link set br-qne down; "
        f"sudo ip link set br-qne address {sw_a_mac}; "
        "sudo ip addr flush dev br-qne; "
        f"sudo ip addr add {station_ip}/24 dev br-qne; "
        "sudo ip link set br-qne up; "
        "sudo sysctl -qw net.ipv4.ip_forward=1; "
        "sudo sysctl -qw net.ipv4.conf.all.rp_filter=0; "
        "sudo sysctl -qw net.ipv4.conf.br-qne.rp_filter=0 2>/dev/null; "
        # alice<->bob is routed back out the same interface, so the switch would
        # send ICMP redirects and the path goes flaky (~50% loss) — disable them.
        "sudo sysctl -qw net.ipv4.conf.all.send_redirects=0; "
        "sudo sysctl -qw net.ipv4.conf.br-qne.send_redirects=0 2>/dev/null; "
        f"sudo sysctl -qw net.ipv4.conf.{if_a}.send_redirects=0 2>/dev/null; "
        f"sudo sysctl -qw net.ipv4.conf.{if_b}.send_redirects=0 2>/dev/null; "
        # don't let bridged frames get dropped by ip/nftables (br_netfilter)
        "sudo sysctl -qw net.bridge.bridge-nf-call-iptables=0 2>/dev/null; "
        # BMv2's Docker install usually leaves FORWARD on DROP -> open it
        "sudo iptables -P FORWARD ACCEPT; sudo iptables -F FORWARD",
        quiet=True,
    )
    # station -> endpoints (their NIC MACs are known on their own segments)
    switch.execute(
        f"sudo ip neigh replace 10.10.1.1 lladdr {alice_mac} nud permanent dev br-qne; "
        f"sudo ip neigh replace 10.10.1.2 lladdr {bob_mac} nud permanent dev br-qne",
        quiet=True,
    )
    # endpoints -> station via their switch-side port MAC (== br-qne's MAC on the
    # alice side; a local FDB entry on the bob side) — always known to FABRIC OVS
    alice.execute(
        f"sudo ip neigh replace {station_ip} lladdr {sw_a_mac} nud permanent dev {alice_iface}; "
        "sudo sysctl -qw net.ipv4.conf.all.accept_redirects=0; "
        f"sudo sysctl -qw net.ipv4.conf.{alice_iface}.accept_redirects=0 2>/dev/null; "
        "sudo ip route flush cache",
        quiet=True,
    )
    bob.execute(
        f"sudo ip neigh replace {station_ip} lladdr {sw_b_mac} nud permanent dev {bob_iface}; "
        "sudo sysctl -qw net.ipv4.conf.all.accept_redirects=0; "
        f"sudo sysctl -qw net.ipv4.conf.{bob_iface}.accept_redirects=0 2>/dev/null; "
        "sudo ip route flush cache",
        quiet=True,
    )

    print("  Testing connectivity (the three links the repeater needs)...")
    checks = [("alice", alice, station_ip),   # ar link: Alice -> repeater station
              ("alice", alice, "10.10.1.2"),  # ab link: Alice -> Bob (routed via station)
              ("switch", switch, "10.10.1.2")]  # rb link: station -> Bob (heralds)
    bad = []
    for who, node, target in checks:
        stdout, _ = node.execute(f"ping -c 3 -W 2 {target}", quiet=True)
        good = "0 received" not in stdout and "100% packet loss" not in stdout
        print(f"    {who} -> {target}: {'ok' if good else 'FAILED'}")
        if not good:
            bad.append(f"{who}->{target}")
    if bad:
        raise RuntimeError(
            f"repeater station connectivity failed for: {', '.join(bad)}. "
            "Inspect on the switch: `ip addr show br-qne`, `ip -br link show master "
            "br-qne`, `sudo iptables -S FORWARD`. Do NOT run the 3-process chain "
            "until all three links ping.")
    print("  All three links reachable — ready for run_sequence_repeater.")
    return station_ip


def run_sequence_bb84(slice_obj, *, num_pulses=20000, key_length=256,
                      fidelity=0.95, efficiency=0.8, dark_count_rate=10.0,
                      distance_km=1.0, attenuation=0.2, sample_fraction=0.2,
                      photon_mode="bulk", photon_drain_ms=500, port=5100,
                      bob_data_ip="10.10.1.2", venv=".venv-qne",
                      transport="raw", loss="auto", photon_rate_hz=10000.0,
                      eve_fraction=0.0, reconcile=True, cascade_passes=4,
                      auth_key=None, finite_key=False, basis_bias=0.5,
                      dead_time=0.0, timing_jitter=0.0, pulse_period_ns=0.0,
                      decoy=False, mu_signal=0.6, mu_decoy=0.1, mu_vacuum=0.001,
                      decoy_probs="0.7,0.2,0.1", channel_delay="auto",
                      classical_transport="tcp", epoch_ns=0):
    """Run distributed-SeQUeNCe BB84 across the slice (raw 0x7101 photons via P4).

    Runs real SeQUeNCe QKDNode/BB84 instances (via `qne_sequence.node_runner`) on
    alice and bob: Bob listens (TCP classical + raw photon RX), Alice connects and
    emits 0x7101 photon frames that traverse the BMv2 P4 switch (which applies the
    fiber-loss drop set by `configure_switch`). Classical sifting/QBER-disclosure
    rides TCP over the data plane — the real-WAN lever.

    Loss is the switch's job; pass matching distance/attenuation only so the
    *reported* analytical loss lines up. Returns (alice_result, bob_result) dicts.

    Prereqs (notebook 01 + this notebook's earlier cells): slice up, BMv2 running
    with the loss table set, data-plane IPs/ARP/promisc configured, and
    setup_sequence_runtime() done.
    """
    import json as _json

    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")

    # Photon-plane args differ by transport:
    #   raw   -> real 0x7101 frames on the data-plane ifaces (loss=switch needs BMv2,
    #            loss=model needs no switch); set MACs for FABRIC OVS delivery.
    #   tcp   -> photon descriptors over the same TCP link as the classical channel;
    #            NO switch, NO raw socket, NO root, NO photon ifaces/MACs.
    # The L2 classical channel (raw 0x7102 through the switch) needs (a) raw quantum
    # mode so --photon-iface is set (both wavelengths share the data-plane iface) and
    # (b) the BMv2 switch in the path. Alice and Bob sit on SEPARATE L2 segments
    # (net_alice_switch, net_switch_bob) joined only by the switch, so the
    # bidirectional 0x7102 channel needs BMv2's classical_channel_params table to
    # bridge them — a no-switch (loss=model/none) run has no such bridge, and each
    # node's peer lives on a segment it cannot reach directly.
    if classical_transport == "l2":
        if transport != "raw":
            raise ValueError("classical_transport='l2' requires transport='raw' "
                             "(the 0x7102 classical channel shares the photon iface)")
        if loss not in ("switch", "auto"):
            raise ValueError(
                "classical_transport='l2' requires the BMv2 switch in the path "
                "(loss='switch' or 'auto'): Alice and Bob are on separate L2 "
                "segments the switch must bridge for the bidirectional 0x7102 "
                f"channel; got loss={loss!r}")

    alice_photon_args = bob_photon_args = ""
    if transport == "raw":
        alice_iface = alice.get_interface(network_name="net_alice_switch").get_device_name()
        bob_iface = bob.get_interface(network_name="net_switch_bob").get_device_name()
        alice_mac = alice.get_interface(network_name="net_alice_switch").get_mac()
        # dst MAC: switch's Alice-side MAC (P4) so FABRIC OVS delivers; for a direct
        # no-switch link (model/none), point dst at Bob's NIC MAC instead.
        if loss in ("model", "none"):
            dst_mac = bob.get_interface(network_name="net_switch_bob").get_mac()
        else:
            dst_mac = slice_obj.get_node("switch").get_interface(
                network_name="net_alice_switch").get_mac()
        alice_photon_args = (f"--photon-iface {alice_iface} --src-mac {alice_mac} "
                             f"--dst-mac {dst_mac}")
        bob_photon_args = f"--photon-iface {bob_iface}"
        print(f"\n=== Running distributed-SeQUeNCe BB84 (raw 0x7101, loss={loss}) ===")
        print(f"  Alice iface {alice_iface} src {alice_mac} -> dst {dst_mac}")
        # The L2 classical channel is bidirectional, so BOB also transmits raw
        # frames and needs real MACs (Alice's photon --src/--dst already double as
        # her classical MACs; in the switch mode l2 requires, her dst is the switch's
        # Alice-side port MAC). l2 is guarded to switch mode above, so Bob's dst is
        # his OWN switch-side port MAC — a real MAC on Bob's own segment
        # (net_switch_bob), never a peer MAC on a segment Bob can't reach. A
        # placeholder dst would be dropped by FABRIC OVS before reaching BMv2.
        if classical_transport == "l2":
            bob_mac = bob.get_interface(network_name="net_switch_bob").get_mac()
            bob_dst_mac = slice_obj.get_node("switch").get_interface(
                network_name="net_switch_bob").get_mac()
            bob_photon_args += f" --src-mac {bob_mac} --dst-mac {bob_dst_mac}"
            print(f"  Bob   iface {bob_iface} src {bob_mac} -> dst {bob_dst_mac} "
                  "(L2 classical)")
    else:
        print("\n=== Running distributed-SeQUeNCe BB84 (tcp descriptors, no switch) ===")
    print(f"  Bob classical+photon TCP {bob_data_ip}:{port} | pulses={num_pulses} "
          f"key_length={key_length} mode={photon_mode} F={fidelity} eff={efficiency}")

    for node in (alice, bob):
        node.execute("sudo pkill -f qne_sequence.node_runner 2>/dev/null; "
                     "sudo rm -f /tmp/seq_*.log; sleep 1", quiet=True)

    common = (f"--num-pulses {num_pulses} --key-length {key_length} "
              f"--fidelity {fidelity} --efficiency {efficiency} "
              f"--dark-count-rate {dark_count_rate} --distance-km {distance_km} "
              f"--attenuation {attenuation} --sample-fraction {sample_fraction} "
              f"--photon-mode {photon_mode} --quantum-transport {transport} --loss {loss} "
              f"--classical-transport {classical_transport} "
              f"--photon-drain-ms {photon_drain_ms} --photon-rate-hz {photon_rate_hz} "
              f"--eve-fraction {eve_fraction} --cascade-passes {cascade_passes} "
              f"{'--reconcile' if reconcile else '--no-reconcile'} "
              f"--basis-bias {basis_bias} --dead-time {dead_time} "
              f"--timing-jitter {timing_jitter} --pulse-period-ns {pulse_period_ns} "
              f"--channel-delay {channel_delay} "
              f"--epoch-ns {epoch_ns} "
              f"--port {port}")
    if auth_key:
        common += f" --auth-key {auth_key}"
    if finite_key:
        common += " --finite-key"
    if decoy:
        common += (f" --decoy --mu-signal {mu_signal} --mu-decoy {mu_decoy} "
                   f"--mu-vacuum {mu_vacuum} --decoy-probs {decoy_probs}")
    # raw sockets need root; env sets PYTHONPATH for `import qne`; cwd holds qne_sequence
    runner = (f"cd ~/qfabric/qne-sequence && sudo env PYTHONPATH=$HOME/qfabric "
              f"$HOME/qfabric/{venv}/bin/python -m qne_sequence.node_runner")

    print("  Starting Bob...")
    bob_thread = bob.execute_thread(
        f"{runner} --role bob --name bob --peer alice --host 0.0.0.0 "
        f"{bob_photon_args} {common} 2>&1 | tee /tmp/seq_bob.log")
    time.sleep(10)  # let Bob open the TCP listener (+ raw RX socket in raw mode)

    print("  Starting Alice (sender)...")
    alice_thread = alice.execute_thread(
        f"{runner} --role alice --name alice --peer bob --host {bob_data_ip} "
        f"{alice_photon_args} {common} 2>&1 | tee /tmp/seq_alice.log")

    print("  Waiting for Alice...")
    a_out = alice_thread.result()
    time.sleep(5)
    try:
        b_out = bob_thread.result()
    except Exception as e:
        b_out = ("", str(e))

    def _parse(out):
        for line in reversed(str(out[0]).splitlines()):
            line = line.strip()
            if line.startswith("{") and '"role"' in line:
                try:
                    return _json.loads(line)
                except _json.JSONDecodeError:
                    pass
        return None

    a_res, b_res = _parse(a_out), _parse(b_out)

    results_dir = PROJECT_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    if a_res:
        (results_dir / "fabric_seq_alice.json").write_text(_json.dumps(a_res, indent=2))
    if b_res:
        (results_dir / "fabric_seq_bob.json").write_text(_json.dumps(b_res, indent=2))

    print("\n=== Distributed-SeQUeNCe BB84 Results ===")
    for who, r in (("alice", a_res), ("bob", b_res)):
        if r:
            print(f"  [{who}] transport={r.get('quantum_transport')}"
                  f"/{r.get('classical_transport')} "
                  f"qber={r.get('qber')} sifted={r.get('sifted_bits')} "
                  f"reconciled={r.get('reconciled')} corrections={r.get('corrections')} "
                  f"leaked={r.get('bits_leaked')} secure_key_bits={r.get('secure_key_bits')} "
                  f"secure_fraction={r.get('secure_fraction')} "
                  f"eve_fraction={r.get('eve_fraction')} "
                  f"key={'yes' if r.get('key') is not None else 'no'} "
                  f"remote_access_errors={r.get('remote_access_errors')}")
        else:
            print(f"  [{who}] no JSON result — check /tmp/seq_{who}.log on the node")
    if a_res and b_res and a_res.get("key") is not None:
        print(f"  keys match bit-for-bit: {a_res['key'] == b_res['key']}")
    return a_res, b_res


def run_sequence_e91(slice_obj, *, num_pairs=20000, fidelity=0.98,
                     distance_km=1.0, attenuation=0.2, mode="e91",
                     sample_fraction=0.2, reconcile=True, port=5100,
                     bob_data_ip="10.10.1.2", venv=".venv-qne",
                     auth_key=None, finite_key=False):
    """Run distributed E91/BBM92 entanglement-based QKD across the slice.

    Unlike BB84, entanglement has **no photon plane / no P4 switch**: Alice hosts
    the shared quantum-state service (the entangled register), Bob measures his
    halves via RPC, and only the classical coordination (basis announcement, QBER
    sample + CHSH bits) rides the real data-plane link — that WAN path is the
    research lever. Fiber loss (distance/attenuation) is applied as pair loss in
    the service, so ``distance_km``/``attenuation`` still shape the detected count.

    ``mode`` is 'e91' (adds the CHSH Bell test) or 'bbm92' (Z/X, key-efficient).
    Prereqs: slice up with data-plane IPs (notebook 01) and setup_sequence_runtime().
    Returns (alice_result, bob_result) dicts.
    """
    import json as _json

    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")

    print(f"\n=== Running distributed E91/BBM92 ({mode}, no switch) ===")
    print(f"  Bob classical TCP {bob_data_ip}:{port} | pairs={num_pairs} "
          f"F={fidelity} dist={distance_km}km atten={attenuation}dB/km "
          f"sample_frac={sample_fraction}")

    for node in (alice, bob):
        node.execute("sudo pkill -f qne_sequence.node_runner 2>/dev/null; "
                     "sudo rm -f /tmp/e91_*.log; sleep 1", quiet=True)

    common = (f"--protocol {mode} --num-pairs {num_pairs} --fidelity {fidelity} "
              f"--distance-km {distance_km} --attenuation {attenuation} "
              f"--sample-fraction {sample_fraction} "
              f"{'--reconcile' if reconcile else '--no-reconcile'} --port {port}")
    if auth_key:
        common += f" --auth-key {auth_key}"
    if finite_key:
        common += " --finite-key"
    # no raw sockets / no root needed — entanglement uses only the TCP link
    runner = (f"cd ~/qfabric/qne-sequence && env PYTHONPATH=$HOME/qfabric "
              f"$HOME/qfabric/{venv}/bin/python -m qne_sequence.node_runner")

    print("  Starting Bob (listener)...")
    bob_thread = bob.execute_thread(
        f"{runner} --role bob --name bob --peer alice --host 0.0.0.0 "
        f"{common} 2>&1 | tee /tmp/e91_bob.log")
    time.sleep(8)  # let Bob open the TCP listener

    print("  Starting Alice (state-service host)...")
    alice_thread = alice.execute_thread(
        f"{runner} --role alice --name alice --peer bob --host {bob_data_ip} "
        f"{common} 2>&1 | tee /tmp/e91_alice.log")

    print("  Waiting for Alice...")
    a_out = alice_thread.result()
    time.sleep(5)
    try:
        b_out = bob_thread.result()
    except Exception as e:
        b_out = ("", str(e))

    def _parse(out):
        for line in reversed(str(out[0]).splitlines()):
            line = line.strip()
            if line.startswith("{") and '"role"' in line:
                try:
                    return _json.loads(line)
                except _json.JSONDecodeError:
                    pass
        return None

    a_res, b_res = _parse(a_out), _parse(b_out)

    results_dir = PROJECT_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    if a_res:
        (results_dir / "fabric_e91_alice.json").write_text(_json.dumps(a_res, indent=2))
    if b_res:
        (results_dir / "fabric_e91_bob.json").write_text(_json.dumps(b_res, indent=2))

    print(f"\n=== Distributed E91/BBM92 Results ({mode}) ===")
    for who, r in (("alice", a_res), ("bob", b_res)):
        if r:
            print(f"  [{who}] detected={r.get('detected_pairs')} "
                  f"sifted={r.get('sifted_bits')} qber={r.get('qber')} "
                  f"CHSH_S={r.get('chsh_s')} reconciled={r.get('reconciled')} "
                  f"corrections={r.get('corrections')} leaked={r.get('bits_leaked')} "
                  f"secure_key_bits={r.get('secure_key_bits')} "
                  f"key={'yes' if r.get('key') is not None else 'no'}")
        else:
            print(f"  [{who}] no JSON result — check /tmp/e91_{who}.log on the node")
    if a_res and b_res and a_res.get("key") is not None:
        print(f"  keys match bit-for-bit: {a_res['key'] == b_res['key']}")
    return a_res, b_res


def run_sequence_repeater(slice_obj, *, num_pairs=20000, fidelity=0.95,
                          distance_km=1.0, attenuation=0.2, chain_mode="bbm92",
                          sample_fraction=0.2, reconcile=True, cascade_passes=4,
                          port=5100, station_ip="10.10.1.3",
                          bob_data_ip="10.10.1.2", venv=".venv-qne",
                          auth_key=None, finite_key=False,
                          apply_correction=True, num_stations=1,
                          channel_delay="auto"):
    """Run the entanglement-swapping repeater chain across the slice.

    The repeater STATION(s) run on the switch node, physically between the
    endpoints, so the classical links traverse real FABRIC segments:
    alice<->station (swap plan + BSM RPCs), station->bob (the HERALDS — the
    multi-hop latency lever), and alice<->bob (sift/QBER/CHSH + Cascade+PA,
    kernel-forwarded through the station).

    ``num_stations`` > 1 runs a longer chain (K stations -> K+2 nodes, K+1
    links) with the extra station processes CO-LOCATED on the switch node —
    real processes and real herald links, one shared middle host. Chains with
    stations on distinct sites need a bigger slice topology.

    ``chain_mode``: 'bbm92' (Z/X key) or 'e91' (adds the CHSH Bell test across
    the swapped chain). ``distance_km``/``attenuation`` set the per-LINK pair
    loss. Prereqs: upload_project, setup_dataplane_ips, setup_repeater_bridge
    (replaces BMv2 with the station bridge), and
    setup_sequence_runtime(slice, nodes=("alice", "bob", "switch")).

    Returns (alice_result, bob_result, repeater_results) — the last is a list
    with one entry per station.
    """
    import json as _json

    alice = slice_obj.get_node("alice")
    bob = slice_obj.get_node("bob")
    switch = slice_obj.get_node("switch")
    port_ar, port_rb = port + 1, port + 2

    print(f"\n=== Running distributed repeater chain ({chain_mode}, "
          f"{num_stations} station(s) on switch node) ===")
    print(f"  links: alice->{station_ip}:{port_ar}.. (BSM), "
          f"station(s)->{bob_data_ip}:{port_rb}.. (heralds), "
          f"alice->{bob_data_ip}:{port} (QKD tail)")
    print(f"  pairs={num_pairs} F={fidelity} per-link dist={distance_km}km "
          f"atten={attenuation}dB/km sample_frac={sample_fraction}")

    for node in (alice, bob, switch):
        node.execute("sudo pkill -f qne_sequence.node_runner 2>/dev/null; "
                     "sudo rm -f /tmp/rep_*.log; sleep 1", quiet=True)

    common = (f"--protocol repeater --chain-mode {chain_mode} "
              f"--num-pairs {num_pairs} --fidelity {fidelity} "
              f"--distance-km {distance_km} --attenuation {attenuation} "
              f"--sample-fraction {sample_fraction} "
              f"--cascade-passes {cascade_passes} "
              f"{'--reconcile' if reconcile else '--no-reconcile'} "
              f"{'' if apply_correction else '--no-correction '}"
              f"--num-stations {num_stations} "
              f"--channel-delay {channel_delay} "
              f"--port {port} --port-ar {port_ar} --port-rb {port_rb}")
    if auth_key:
        common += f" --auth-key {auth_key}"
    if finite_key:
        common += " --finite-key"
    # no raw sockets / no root needed — the chain uses only the TCP links
    runner = (f"cd ~/qfabric/qne-sequence && env PYTHONPATH=$HOME/qfabric "
              f"$HOME/qfabric/{venv}/bin/python -m qne_sequence.node_runner")

    print("  Starting Bob (listens for alice + stations)...")
    bob_thread = bob.execute_thread(
        f"{runner} --role bob --name bob --host 0.0.0.0 --seed 2 "
        f"{common} 2>&1 | tee /tmp/rep_bob.log")
    time.sleep(8)

    print(f"  Starting {num_stations} repeater station(s) on the switch node...")
    station_cmds = " & ".join(
        f"{runner} --role repeater --name station{i} --station-index {i} "
        f"--host 0.0.0.0 --seed {2 + i} --bob-host {bob_data_ip} "
        f"{common} > /tmp/rep_station{i}.log 2>&1"
        for i in range(1, num_stations + 1))
    rep_thread = switch.execute_thread(f"{station_cmds} & wait")
    time.sleep(8)

    print("  Starting Alice (register host)...")
    alice_thread = alice.execute_thread(
        f"{runner} --role alice --name alice --seed 1 "
        f"--host {bob_data_ip} --bob-host {bob_data_ip} "
        f"--repeater-host {station_ip} "
        f"{common} 2>&1 | tee /tmp/rep_alice.log")

    print("  Waiting for the chain to complete...")
    a_out = alice_thread.result()
    time.sleep(5)
    outs = {"alice": a_out}
    for who, th in (("bob", bob_thread), ("stations", rep_thread)):
        try:
            outs[who] = th.result()
        except Exception as e:
            outs[who] = ("", str(e))

    def _parse(text):
        for line in reversed(str(text).splitlines()):
            line = line.strip()
            if line.startswith("{") and '"role"' in line:
                try:
                    return _json.loads(line)
                except _json.JSONDecodeError:
                    pass
        return None

    a_res = _parse(outs["alice"][0])
    b_res = _parse(outs["bob"][0])
    # station outputs went to per-station log files (they ran backgrounded)
    m_res = []
    for i in range(1, num_stations + 1):
        log, _ = switch.execute(f"tail -1 /tmp/rep_station{i}.log", quiet=True)
        r = _parse(log)
        if r:
            m_res.append(r)

    results_dir = PROJECT_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    if a_res:
        (results_dir / "fabric_repeater_alice.json").write_text(_json.dumps(a_res, indent=2))
    if b_res:
        (results_dir / "fabric_repeater_bob.json").write_text(_json.dumps(b_res, indent=2))
    for r in m_res:
        idx = r.get("station_index", 1)
        (results_dir / f"fabric_repeater_station{idx}.json").write_text(
            _json.dumps(r, indent=2))

    print(f"\n=== Distributed Repeater Results ({chain_mode}, "
          f"{num_stations + 2} nodes / {num_stations + 1} links) ===")
    for who, r in (("alice", a_res), ("bob", b_res)):
        if r:
            print(f"  [{who}] delivered={r.get('delivered')}/{r.get('attempts')} "
                  f"qber={r.get('qber')} (pred {r.get('qber_pred'):.4f}) "
                  f"CHSH_S={r.get('chsh_s')} (pred {r.get('chsh_pred'):.3f}) "
                  f"reconciled={r.get('reconciled')} "
                  f"secure_key_bits={r.get('secure_key_bits')} "
                  f"key={'yes' if r.get('key') is not None else 'no'}")
        else:
            print(f"  [{who}] no JSON result — check /tmp/rep_{who}.log on the node")
    for r in m_res:
        print(f"  [station{r.get('station_index')}] swaps={r.get('swaps')} "
              f"heralds={r.get('heralds')} tx={r.get('tx_frames')} rx={r.get('rx_frames')}")
    if len(m_res) < num_stations:
        print(f"  WARNING: only {len(m_res)}/{num_stations} station results — "
              "check /tmp/rep_station*.log on the switch")
    if a_res and b_res and a_res.get("key") is not None:
        print(f"  keys match bit-for-bit: {a_res['key'] == b_res['key']}")
    return a_res, b_res, m_res


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
    parser.add_argument("--site-bob", default="TACC",
                        help="FABRIC site for Bob (default: same site as Alice — "
                             "single-site slice, distance is emulated; pick a "
                             "remote site only for classical stress runs)")
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
    print("To clean up: python scripts/deploy_fabric.py --cleanup")


if __name__ == "__main__":
    main()
