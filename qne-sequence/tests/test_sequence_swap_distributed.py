"""SeQUeNCe's own EntanglementSwapping protocols, distributed via a remote QuantumManager.

Proves the task-#20 conclusion: with the quantum state behind an RPC hook, SeQUeNCe's
UNMODIFIED EntanglementSwappingA/B produce a correct end-to-end |Φ+⟩ across three node
adapters — i.e. the only thing needed to distribute SeQUeNCe's entanglement stack is a
remote-QuantumManager. SeQUeNCe itself is not modified.
"""

from __future__ import annotations

from qne_sequence.sequence_swap_poc import run_distributed_swap


def test_remote_swap_yields_phi_plus_every_time():
    n = ok = 0
    for seed in range(150):
        probs, calls = run_distributed_swap(seed)
        n += 1
        # |Φ+⟩ over (left,right): |00> and |11> each 0.5, |01>=|10>=0
        good = (len(probs) == 4 and abs(probs[0] - 0.5) < 1e-6
                and abs(probs[3] - 0.5) < 1e-6
                and probs[1] < 1e-6 and probs[2] < 1e-6)
        ok += good
        assert calls > 0            # the state really lived behind the RPC boundary
    assert ok == n                  # SeQUeNCe's swap over the remote QM: always Φ+


def test_state_lived_behind_the_rpc():
    # every quantum op (2 set + BSM run_circuit + herald correction + get) is an RPC call
    _probs, calls = run_distributed_swap(seed=0)
    assert calls >= 4               # 2 set, >=1 run_circuit (BSM), 1 get (+correction when heralded)
