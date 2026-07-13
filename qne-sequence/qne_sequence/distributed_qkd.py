"""DistributedBB84 — BB84 with every `another` poke converted to a message (§8.1).

Stock ``sequence.qkd.BB84`` reaches across into the peer protocol's memory in ~10
places (audited in DESIGN.md §8.1): it writes the peer's buffers in ``push`` /
``start_protocol``, pops the peer's lists in ``begin_photon_pulse``, calls
``another.set_key()`` / ``another._pop()`` in the MATCHING_INDICES handler, and reads
the peer's *secret key* (``self.key ^ self.another.key``) to compute QBER.

This subclass overrides exactly those methods so each side touches only its own
state and coordinates via messages. The only surviving uses of ``self.another`` are
``self.another.name`` and ``self.another.owner.name`` (addressing) — which the
GuardedRemoteStub permits. Anything we missed raises RemoteAccessError at runtime.

Phase B (current):
  * Quantum channel is descriptor-on-wire with **fiber loss** (RemoteQuantumChannel).
  * Bob measures with qfabric's validated **Detector** (efficiency, dark counts,
    polarization error) so QBER matches qfabric's cross-validated model.
  * The in-process cheat ``self.key ^ self.another.key`` is replaced by **sample
    disclosure** over the classical channel (Bob reveals a random sample of his
    sifted bits; Alice estimates QBER) — reusing ``qne.bb84.BB84Protocol`` math.
  * key_num == 1 per request; no error correction yet (Cascade is later), so with a
    non-zero QBER Alice's and Bob's keys differ on the error positions by design.

Control-message flow (all real traffic on the wire):
  Alice  push -> start_protocol --BEGIN_PHOTON_PULSE-->  Bob (inits own buffers)
  Alice  begin_photon_pulse --QUBITS(lossy)---------->   Bob.receive_qubits (detects)
  Bob    --RECEIVED_QUBITS-->                            Alice
  Alice  --BASIS_LIST-------->                           Bob (sifts; samples for QBER)
  Bob    --SIFTED(indices+disclosed sample)-->          Alice (estimates QBER, keys)
  Alice  --QBER_RESULT-->                                Bob (finalizes own key)
"""

from __future__ import annotations

import types
from time import time_ns

import numpy

from sequence.qkd.BB84 import BB84
from sequence.kernel.event import Event
from sequence.kernel.process import Process

from qne.bb84 import BB84Protocol

from .wire_codec import WireMessage
from .guarded_stub import GuardedRemoteStub
from .photon_path import make_strategy

# Stand-in for run_time=inf that survives JSON (inf is not standard JSON).
_NEVER = 10 ** 18


def pair_distributed(protocol: "DistributedBB84", role: int,
                     peer_proto_name: str, peer_node_name: str) -> None:
    """Distributed analogue of pair_bb84_protocols.

    Instead of wiring two live objects together, set this side's role and install a
    GuardedRemoteStub as ``another`` so only legitimate addressing reads succeed.
    """
    protocol.role = role
    protocol.another = GuardedRemoteStub(peer_proto_name, peer_node_name)


class DistributedBB84(BB84):
    def __init__(self, owner, name, lightsource, qsdetector, role=-1,
                 seed: int = 0, on_key=None, detector=None,
                 sample_fraction: float = 0.1, num_pulses: int | None = None,
                 photon_mode: str = "bulk", photon_drain_ps: int = 0,
                 eavesdropper=None, basis_bias: float = 0.5, decoy=None):
        super().__init__(owner, name, lightsource, qsdetector, role)
        # QKDNode.receive_message (sequence 1.0.0) routes to the first protocol whose
        # protocol_type is truthy; the base Protocol leaves it "" (falsy). Set it so
        # our single distributed protocol receives all inbound messages.
        self.protocol_type = "DistributedBB84"
        self.rng = numpy.random.default_rng(seed)
        self.on_key = on_key            # callback(role, info_dict) when a key completes
        self.detector = detector        # qne.detector.Detector (Bob); None on Alice
        self.eavesdropper = eavesdropper  # qne.eve.InterceptResendEve on the channel; None = no Eve
        # P(Z basis). 0.5 = standard BB84. Biased (efficient BB84): key from Z–Z
        # matches, ALL X–X matches disclosed for phase-error estimation.
        self.basis_bias = basis_bias
        # Decoy-state source (weak-coherent, PNS-resilient): dict with
        # "intensities" {signal,decoy,vacuum -> mu}, "probs" [P(s),P(d),P(v)],
        # "loss_probability" (folded into per-photon binomial thinning here, so
        # the channel itself must be lossless), and optional "f_ec".
        # Key material comes from SIGNAL pulses only; decoy-class matches are
        # fully disclosed to measure E_decoy; gains come from Bob's detected set.
        self.decoy = decoy
        if decoy is not None:
            if basis_bias != 0.5:
                raise ValueError("decoy mode does not compose with --basis-bias")
            if photon_mode != "bulk":
                raise ValueError("decoy mode requires photon_mode='bulk'")
        self._decoy_classes: list[int] = []          # per-pulse 0=signal/1=decoy/2=vacuum
        self.sample_fraction = sample_fraction
        self.num_pulses_override = num_pulses
        self.photon_mode = photon_mode
        self.strategy = make_strategy(photon_mode)
        # raw mode: photons (P4 path) race QUBITS_DONE (TCP); wait this long for
        # stragglers before Bob proceeds. 0 for tcp mode (one ordered link).
        self.photon_drain_ps = photon_drain_ps

        self.final_keys: list[int] = []
        self.metrics: dict = {}
        self._bob_records: dict[int, tuple[int, int]] = {}  # seq -> (basis, bit)
        self._bob_key_order: list[int] = []
        self._bob_sifted_count = 0
        self._bob_num_sampled = 0
        self._bob_detected = 0
        # raw mode head race: photons (P4 path) can arrive BEFORE the TCP
        # BEGIN_PHOTON_PULSE that resets _bob_records; buffer them here and
        # replay after BEGIN instead of silently wiping the head of the train.
        self._pre_begin_pulses: list = []
        self._num_pulses = 0
        self._t_start_ns = 0

    # -- addressing helpers (the only permitted `another` reads) ---------------

    @property
    def peer_node(self) -> str:
        return self.another.owner.name

    @property
    def peer_proto(self) -> str:
        return self.another.name

    def _send(self, msg_type: str, payload: dict) -> None:
        msg = WireMessage(msg_type, self.peer_proto, payload)
        self.owner.send_message(self.peer_node, msg, priority=0)

    # -- Alice: key request ----------------------------------------------------

    def push(self, length: int, key_num: int, run_time: int = _NEVER) -> None:
        if self.role != 0:
            raise AssertionError("push (generate key) must be called from Alice")
        self._t_start_ns = time_ns()    # wall-clock start for throughput metrics
        # local only — Bob initializes his own buffers on BEGIN_PHOTON_PULSE
        self.key_lengths.append(length)
        self.keys_left_list.append(key_num)
        self.end_run_times.append(run_time + self.owner.timeline.now())
        if self.ready:
            self.ready = False
            self.working = True
            self.start_protocol()

    def start_protocol(self) -> None:
        if not self.key_lengths:
            self.ready = True
            return
        # reset own buffers only (stock BB84 also reset self.another.* here)
        self.basis_lists = []
        self.bit_lists = []
        self.key_bits = []
        self.latency = 0
        self.working = True

        ls = self.owner.components[self.ls_name]
        self.ls_freq = ls.frequency
        self.light_time = self.key_lengths[0] / (self.ls_freq * ls.mean_photon_num)
        cc = self.owner.cchannels[self.peer_node]
        self.start_time = int(self.owner.timeline.now()) + round(cc.delay)

        self._send("BEGIN_PHOTON_PULSE", {
            "frequency": self.ls_freq,
            "light_time": self.light_time,
            "start_time": self.start_time,
            "wavelength": ls.wavelength,
            "end_run_time": self.end_run_times[0],   # Bob sets his own guard window
            "key_length": self.key_lengths[0],
        })

        # emit the photon batch as a scheduled local event
        self.owner.timeline.schedule(
            Event(self.start_time, Process(self, "begin_photon_pulse", [])))
        self.last_key_time = self.owner.timeline.now()

    def begin_photon_pulse(self) -> None:
        if not self.working:
            return
        num_pulses = self.num_pulses_override or round(self.light_time * self.ls_freq)
        self._num_pulses = num_pulses
        # P(Z) = basis_bias; 0.5 reproduces the unbiased integers(0, 2) draw
        basis_list = (self.rng.random(num_pulses) >= self.basis_bias).astype(int)
        bit_list = self.rng.integers(0, 2, num_pulses)
        self.basis_lists.append(basis_list)
        self.bit_lists.append(bit_list)
        # delegate transport granularity to the selected strategy (DESIGN §4.3)
        self.strategy.emit(self, basis_list, bit_list)

    def make_pulses(self, basis_list, bit_list) -> list:
        """Build the wire descriptors for one pulse train (BulkStream hook).

        Standard mode: [seq, basis, bit]. Decoy mode: [seq, basis, bit, n] where
        n is the photon count that SURVIVES the fiber — the source draws
        Poisson(μ_class) photons and the channel thins them Binomial(n, 1−loss).
        Every pulse ships (even n = 0): an empty slot can still dark-count.
        """
        num = len(basis_list)
        if self.decoy is None:
            return [[i, int(basis_list[i]), int(bit_list[i])] for i in range(num)]
        mus = self.decoy["intensities"]
        mu_by_class = (mus["signal"], mus["decoy"], mus["vacuum"])
        probs = self.decoy["probs"]
        p_loss = self.decoy.get("loss_probability", 0.0)
        classes = self.rng.choice(3, size=num, p=probs)
        self._decoy_classes = [int(c) for c in classes]
        n_photons = self.rng.poisson([mu_by_class[c] for c in classes])
        n_arriving = self.rng.binomial(n_photons, 1.0 - p_loss)
        return [[i, int(basis_list[i]), int(bit_list[i]), int(n_arriving[i])]
                for i in range(num)]

    def emit_one_photon(self, seq: int, basis: int, bit: int) -> None:
        """PerPhotonEvent: transmit a single photon (one Event, one frame)."""
        self.owner.qchannels[self.peer_node].transmit_one(
            self.owner.name, self.peer_proto, seq, basis, bit)

    def send_quantum_done(self) -> None:
        """Signal end of the photon train (after the last surviving photon)."""
        self._send("QUBITS_DONE", {})

    def _send_received_qubits(self) -> None:
        """Bob acknowledges the photon train (possibly after a drain delay)."""
        self._send("RECEIVED_QUBITS", {})

    # -- Bob: measure the arriving (lossy) photons -----------------------------

    def receive_qubits(self, src: str, pulses: list) -> None:
        """Apply the detector model to each delivered photon and accumulate.

        Called once (BulkStream) or many times (PerPhotonEvent); accumulates into
        _bob_records until QUBITS_DONE arrives. Reset happens on BEGIN_PHOTON_PULSE.
        """
        if not self.working:
            # BEGIN_PHOTON_PULSE hasn't been processed yet (raw-mode head race:
            # the quantum path outran the TCP control path). Don't detect-and-
            # record now — BEGIN would wipe the records; replay after BEGIN.
            self._pre_begin_pulses.extend(pulses)
            return
        # Eve (if present) intercepts in transit: she measures each photon in a
        # random basis and resends her result, so the photon Bob's detector sees
        # carries Eve's (basis, bit). Alice still announces her ORIGINAL basis for
        # sifting, so a wrong-basis interception surfaces as a bit error.
        if self.eavesdropper is not None:
            pulses = self.eavesdropper.intercept_pulses(pulses)
        for seq, a_basis, a_bit, *rest in pulses:
            photon = types.SimpleNamespace(basis=int(a_basis), state=int(a_bit),
                                           sequence_num=int(seq))
            if rest:                      # decoy descriptor: surviving photon count
                ev = self.detector.detect_pulse(photon, int(rest[0]))
            else:
                ev = self.detector.detect(photon)
            if ev.detected:
                self._bob_records[int(seq)] = (int(ev.basis), int(ev.bit_value))

    # -- classical control plane -----------------------------------------------

    def received_message(self, src: str, msg) -> None:
        t = msg.msg_type

        if t == "BEGIN_PHOTON_PULSE":            # current node is Bob
            p = msg.payload
            self.ls_freq = p["frequency"]
            self.light_time = p["light_time"]
            self.start_time = int(p["start_time"])
            self.key_lengths = [p["key_length"]]
            self.end_run_times = [p["end_run_time"]]
            self.keys_left_list = [1]
            self.basis_lists = []
            self.bit_lists = []
            self.key_bits = []
            self._bob_records = {}
            self.working = True
            # Bob now accumulates photons (receive_qubits) until QUBITS_DONE.
            # Replay any photons that beat this BEGIN over the raw path.
            if self._pre_begin_pulses:
                early, self._pre_begin_pulses = self._pre_begin_pulses, []
                self.receive_qubits("", early)

        elif t == "QUBITS_DONE":                 # current node is Bob: train complete
            # In raw mode the photon path (P4) races this TCP marker; give stragglers
            # photon_drain_ps to arrive before proceeding (0 in single-link tcp mode).
            if self.photon_drain_ps > 0:
                fire = self.owner.timeline.now() + self.photon_drain_ps
                self.owner.timeline.schedule(
                    Event(fire, Process(self, "_send_received_qubits", [])))
            else:
                self._send_received_qubits()

        elif t == "RECEIVED_QUBITS":             # current node is Alice
            bases = self.basis_lists[0]
            payload = {"bases": [int(x) for x in bases]}
            if self.decoy is not None:
                # intensity labels are public AFTER Bob's detections are locked in
                payload["intensity"] = self._decoy_classes
            self._send("BASIS_LIST", payload)

        elif t == "BASIS_LIST":                  # current node is Bob: sift + sample
            alice_bases = msg.payload["bases"]
            matching = sorted(
                seq for seq, (bb, _bit) in self._bob_records.items()
                if seq < len(alice_bases) and bb == alice_bases[seq]
            )
            self._bob_detected = len(self._bob_records)
            decoy_extra = {}
            classes = msg.payload.get("intensity")
            if classes is not None:
                # decoy mode: key from SIGNAL pulses only; decoy-class matches are
                # fully disclosed (E_decoy); every detection feeds the gain stats
                decoy_matched = [s for s in matching if classes[s] == 1]
                matching = [s for s in matching if classes[s] == 0]
                decoy_extra = {
                    "detected_sequences": sorted(self._bob_records),
                    "decoy_indices": decoy_matched,
                    "decoy_bits": [self._bob_records[s][1] for s in decoy_matched],
                }
            if self.basis_bias != 0.5:
                # efficient BB84: key from Z–Z matches; disclose ALL X–X matches
                # (phase-error estimate) + a sample of Z–Z (bit-error estimate)
                matching_z = [s for s in matching if self._bob_records[s][0] == 0]
                matching_x = [s for s in matching if self._bob_records[s][0] == 1]
                n_sample = BB84Protocol.sample_size(len(matching_z), self.sample_fraction)
                z_sample = sorted(self.rng.choice(
                    matching_z, size=n_sample, replace=False).tolist()) if n_sample else []
                sample = sorted(matching_x + z_sample)
            else:
                # choose a random disclosure sample for QBER estimation
                # (sampling policy shared with qne.bb84.BB84Protocol)
                n_sample = BB84Protocol.sample_size(len(matching), self.sample_fraction)
                sample = sorted(self.rng.choice(matching, size=n_sample, replace=False).tolist()) \
                    if n_sample else []
            sample_set = set(sample)
            self._bob_key_order = [s for s in matching if s not in sample_set]
            # keep the full sifted count so Bob's result reports the same
            # semantics as Alice's (sifted_bits = matches, key_bits = remainder)
            self._bob_sifted_count = len(matching)
            self._bob_num_sampled = len(sample)
            self._send("SIFTED", {
                "matching_indices": matching,
                "sample_indices": sample,
                "bob_sample_bits": [self._bob_records[s][1] for s in sample],
                **decoy_extra,
            })

        elif t == "SIFTED":                      # current node is Alice: QBER + key
            matching = msg.payload["matching_indices"]
            sample = msg.payload["sample_indices"]
            bob_sample_bits = msg.payload["bob_sample_bits"]
            alice_bits = self.bit_lists[0]

            # QBER from the disclosed sample (replaces self.key ^ self.another.key);
            # error counting + Wilson CI shared with qne.bb84.BB84Protocol
            a_sample = [int(alice_bits[s]) for s in sample]
            b_sample = [int(b) for b in bob_sample_bits]
            qber_est = BB84Protocol.qber_from_disclosed(a_sample, b_sample)
            errors = qber_est.num_errors
            num_sampled = qber_est.num_sampled

            qber_x = None
            if self.basis_bias != 0.5:
                # efficient BB84: split the disclosed positions by Alice's basis —
                # Z sample = bit-error estimate, X matches = phase-error estimate
                bases = self.basis_lists[0]
                zi = [i for i, s in enumerate(sample) if int(bases[s]) == 0]
                xi = [i for i, s in enumerate(sample) if int(bases[s]) == 1]
                qz = BB84Protocol.qber_from_disclosed(
                    [a_sample[i] for i in zi], [b_sample[i] for i in zi])
                qx = BB84Protocol.qber_from_disclosed(
                    [a_sample[i] for i in xi], [b_sample[i] for i in xi])
                if qz.num_sampled and qx.num_sampled:
                    secure_fraction = BB84Protocol.efficient_secure_fraction(
                        qz.qber, qx.qber)
                else:
                    secure_fraction = 0.0     # unestimated phase error = no security
                qber = qz.qber                # the key (Z) error rate — sizes Cascade
                qber_x = qx.qber
            else:
                qber = qber_est.qber
                secure_fraction = BB84Protocol.secure_key_fraction(qber)

            sample_set = set(sample)
            key_order = [s for s in matching if s not in sample_set]
            self.key_bits = [int(alice_bits[s]) for s in key_order]

            key_int = None
            if len(self.key_bits) >= self.key_lengths[0]:
                self.set_key()
                self._pop(info=self.key)
                self.final_keys.append(self.key)
                key_int = self.key

            final_key_bits = int(len(key_order) * secure_fraction)
            elapsed_s = (time_ns() - self._t_start_ns) / 1e9
            self.metrics = {
                "qber": qber, "num_sampled": num_sampled, "num_errors": errors,
                "qber_ci": list(qber_est.confidence_interval),
                "qber_disclosed": qber_est.qber, "qber_x": qber_x,
                "basis_bias": self.basis_bias,
                "sifted_bits": len(matching), "key_bits": len(key_order),
                "secure_fraction": secure_fraction,
                "final_key_bits": final_key_bits,
                "photon_mode": self.photon_mode, "photons_emitted": self._num_pulses,
                "elapsed_s": elapsed_s,
                "photons_per_s": (self._num_pulses / elapsed_s) if elapsed_s > 0 else None,
            }

            # Decoy-state analysis on the LIVE statistics: measured per-intensity
            # gains (from Bob's full detection set) + measured E_signal/E_decoy
            # feed the Lo–Ma–Chen bounds -> GLLP rate (qne/decoy.py, unchanged).
            decoy_summary = None
            if self.decoy is not None and "detected_sequences" in msg.payload:
                from qne.decoy import decoy_state_key_rate
                labels = ("signal", "decoy", "vacuum")
                classes = self._decoy_classes
                sent = [0, 0, 0]
                for c in classes:
                    sent[c] += 1
                det = [0, 0, 0]
                for s in msg.payload["detected_sequences"]:
                    det[classes[s]] += 1
                gains = {lab: (det[i] / sent[i] if sent[i] else 0.0)
                         for i, lab in enumerate(labels)}
                e_d = BB84Protocol.qber_from_disclosed(
                    [int(alice_bits[s]) for s in msg.payload["decoy_indices"]],
                    [int(b) for b in msg.payload["decoy_bits"]])
                res = decoy_state_key_rate(
                    gains, {"signal": qber, "decoy": e_d.qber},
                    self.decoy["intensities"], f_ec=self.decoy.get("f_ec", 1.16))
                decoy_summary = {
                    "gains": gains,
                    "sent": dict(zip(labels, sent)),
                    "detected": dict(zip(labels, det)),
                    "qber_signal": qber, "qber_decoy": e_d.qber,
                    "Y1_lower": res["Y1_lower"], "e1_upper": res["e1_upper"],
                    "Q1": res["Q1"], "Y0": res["Y0"],
                    "secure_key_rate": res["secure_key_rate"],
                    # per-pulse GLLP rate x pulses sent = decoy-secure key budget
                    "decoy_key_bits": int(res["secure_key_rate"] * self._num_pulses),
                }
                self.metrics["decoy"] = decoy_summary

            self._send("QBER_RESULT", {"qber": qber, "qber_x": qber_x,
                                       "secure_fraction": secure_fraction,
                                       "decoy": decoy_summary})
            self.keys_left_list[0] -= 1
            self.working = False
            if self.on_key:
                self.on_key(self.role, {"key": key_int, **self.metrics})

        elif t == "QBER_RESULT":                 # current node is Bob: finalize key
            qber = msg.payload["qber"]
            self.key_bits = [self._bob_records[s][1] for s in self._bob_key_order]
            key_int = None
            if len(self.key_bits) >= self.key_lengths[0]:
                self.set_key()
                self._pop(info=self.key)
                self.final_keys.append(self.key)
                key_int = self.key
            # Bob now knows QBER too — report the same secure metrics as Alice
            # so both result rows are complete (symmetric for sweeps/plots).
            # Alice already folded the biased-basis math into secure_fraction.
            secure_fraction = msg.payload.get(
                "secure_fraction", BB84Protocol.secure_key_fraction(qber))
            detected = getattr(self, "_bob_detected", 0)
            self.metrics = {"qber": qber,
                            "qber_x": msg.payload.get("qber_x"),
                            "basis_bias": self.basis_bias,
                            "sifted_bits": self._bob_sifted_count,
                            "detected_pulses": detected,
                            "sift_ratio": (self._bob_sifted_count / detected
                                           if detected else None),
                            "num_sampled": self._bob_num_sampled,
                            "key_bits": len(self._bob_key_order),
                            "secure_fraction": secure_fraction,
                            "final_key_bits": int(len(self._bob_key_order) * secure_fraction),
                            "decoy": msg.payload.get("decoy")}
            if self.eavesdropper is not None:
                self.metrics.update(self.eavesdropper.stats)
            self.working = False
            if self.on_key:
                self.on_key(self.role, {"key": key_int, **self.metrics})
