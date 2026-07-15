"""PoC — SeQUeNCe's OWN entanglement-swapping protocols, distributed via a remote
QuantumManager hook. (2026-07-14; for the SeQUeNCe-team follow-up.)

The task-#20 finding: SeQUeNCe's `EntanglementSwappingA/B` (and generation) are already
message-based — the only thing blocking distribution is that every quantum op goes through
one per-process `timeline.quantum_manager`, and linked memories on different nodes must
share one joint state. This module demonstrates the fix at exactly that layer:

  * `QuantumStateService` owns ONE real `sequence` `QuantumManagerKet` and answers wire ops
    (new / set / run_circuit / get) as JSON round-trips.
  * `RemoteQuantumManagerKet` is a duck-typed QuantumManager that a node's
    `timeline.quantum_manager` points at; it forwards every op to the service. (The JSON
    shape is identical to qfabric's socket RpcChannel — it drops onto real sockets
    unchanged; here the transport is a synchronous call for a deterministic, testable PoC.)

With three node adapters all pointing at one service, SeQUeNCe's UNMODIFIED swapping
protocol objects run across the (simulated) process boundary: the middle does the BSM and
sends heralds via `owner.send_message`, the ends apply the Pauli correction — and the
end-to-end pair comes out |Φ+⟩ every time. SeQUeNCe itself is not modified; this is all
qfabric-side glue.

`run_distributed_swap()` returns the end-to-end joint-state probabilities so a test can
assert |Φ+⟩. See tests/test_sequence_swap_distributed.py.
"""

from __future__ import annotations

import json
import math

import numpy as np

from sequence.kernel.quantum_manager import QuantumManagerKet
from sequence.components.circuit import Circuit
from sequence.entanglement_management.swapping.swapping_circuit import (
    EntanglementSwappingA_Circuit,
    EntanglementSwappingB_Circuit,
)

_INV = 1.0 / math.sqrt(2.0)
_PHI_PLUS = [_INV, 0, 0, _INV]


class QuantumStateService:
    """Owns the single real QuantumManagerKet; answers wire ops (JSON in, JSON out)."""

    def __init__(self):
        self.qm = QuantumManagerKet()
        self.calls = 0

    def handle(self, msg: str) -> str:
        self.calls += 1
        req = json.loads(msg)
        op, b = req["op"], req["body"]
        if op == "new":
            return json.dumps({"key": self.qm.new()})
        if op == "set":
            self.qm.set(b["keys"], [complex(re, im) for re, im in b["amps"]])
            return json.dumps({})
        if op == "run_circuit":
            c = Circuit(b["circuit"]["size"])
            c.deserialize(b["circuit"])
            res = self.qm.run_circuit(c, b["keys"], b["meas_samp"])
            return json.dumps({"res": {str(k): int(v) for k, v in res.items()}})
        if op == "get":
            st = self.qm.get(b["key"]).state
            return json.dumps({"state": [[z.real, z.imag] for z in st]})
        raise ValueError(f"unknown op {op!r}")


class RemoteQuantumManagerKet:
    """Duck-typed QuantumManager forwarding new/set/run_circuit to a central service.

    A SeQUeNCe node's ``timeline.quantum_manager`` points here; SeQUeNCe's protocols and
    memories use it unmodified. Swap the ``_send`` transport for qfabric's socket
    RpcChannel to run genuinely cross-process — the payloads are already wire-shaped.
    """

    def __init__(self, service: QuantumStateService):
        self._svc = service

    def _send(self, op, body):
        return json.loads(self._svc.handle(json.dumps({"op": op, "body": body})))

    def new(self, state=None):
        return self._send("new", {})["key"]

    def set(self, keys, amplitudes):
        self._send("set", {"keys": list(keys),
                           "amps": [[complex(a).real, complex(a).imag] for a in amplitudes]})

    def run_circuit(self, circuit, keys, meas_samp=None):
        r = self._send("run_circuit", {"circuit": circuit.serialize(),
                                       "keys": list(keys), "meas_samp": meas_samp})
        return {int(k): v for k, v in r["res"].items()}

    def state_probs(self, key):
        """PoC helper: |amplitude|^2 of the joint group containing ``key``."""
        st = self._send("get", {"key": key})["state"]
        return np.abs(np.array([complex(re, im) for re, im in st])) ** 2


# ------------------------------------------------------------------ minimal harness
class _Memory:
    def __init__(self, adapter, name):
        self.qstate_key = adapter.new()
        self.name = name
        self.fidelity = 1.0
        self.entangled_memory = {"node_id": None, "memo_id": None}
        self._expire = 10 ** 18

    def get_expire_time(self):
        return self._expire

    def update_expire_time(self, t):
        self._expire = t


class _RM:  # resource manager stub — swapping's success path only calls update()
    def update(self, proto, memory, state):
        pass

    def release_remote_protocol(self, node, proto):
        pass

    def release_remote_memory(self, node, memo):
        pass


class _Timeline:
    def __init__(self, qm):
        self.quantum_manager = qm

    def now(self):
        return 0


class _Node:
    def __init__(self, name, adapter, seed, router):
        self.name = name
        self.timeline = _Timeline(adapter)
        self.resource_manager = _RM()
        self._rng = np.random.default_rng(seed)
        self.protocols = {}
        self._router = router

    def get_generator(self):
        return self._rng

    def send_message(self, dst, msg):
        self._router[dst].protocols[msg.receiver].received_message(self.name, msg)


def run_distributed_swap(seed: int = 0):
    """Run SeQUeNCe's swapping protocols over the remote-QM hook once.

    Returns (probs, service_calls): probs = |amplitude|^2 of the end-to-end (left,right)
    joint state (|Φ+⟩ → [0.5, 0, 0, 0.5]); service_calls = number of RPC ops the central
    manager served (evidence the quantum state really lived behind the RPC).
    """
    svc = QuantumStateService()
    router = {}
    a_left = RemoteQuantumManagerKet(svc)
    a_mid = RemoteQuantumManagerKet(svc)
    a_right = RemoteQuantumManagerKet(svc)
    left = _Node("left", a_left, seed + 1, router)
    mid = _Node("mid", a_mid, seed + 2, router)
    right = _Node("right", a_right, seed + 3, router)
    router.update(left=left, mid=mid, right=right)

    mL = _Memory(a_left, "mL")
    mM1 = _Memory(a_mid, "mM1")
    mM2 = _Memory(a_mid, "mM2")
    mR = _Memory(a_right, "mR")
    # two elementary pairs in the central manager (stands in for generation)
    a_mid.set([mL.qstate_key, mM1.qstate_key], _PHI_PLUS)
    a_mid.set([mM2.qstate_key, mR.qstate_key], _PHI_PLUS)
    mL.entangled_memory = {"node_id": "mid", "memo_id": "mM1"}
    mM1.entangled_memory = {"node_id": "left", "memo_id": "mL"}
    mM2.entangled_memory = {"node_id": "right", "memo_id": "mR"}
    mR.entangled_memory = {"node_id": "mid", "memo_id": "mM2"}

    esA = EntanglementSwappingA_Circuit(mid, "esA", mM1, mM2)
    esB_left = EntanglementSwappingB_Circuit(left, "esB_left", mL)
    esB_right = EntanglementSwappingB_Circuit(right, "esB_right", mR)
    esA.left_protocol_name, esA.right_protocol_name = "esB_left", "esB_right"
    esB_left.set_others("esA", "mid", ["mM1"])
    esB_right.set_others("esA", "mid", ["mM2"])
    left.protocols["esB_left"] = esB_left
    right.protocols["esB_right"] = esB_right
    mid.protocols["esA"] = esA

    esA.start()   # BSM + heralds + corrections, all crossing the RPC boundary

    return a_left.state_probs(mL.qstate_key), svc.calls
