"""Cascade reconciliation over a Link — shared by the E91 and BB84 paths.

The implementation moved to ``qne/reconcile.py`` so the hand-coded raw-socket
path (qne/alice.py, qne/bob.py) reconciles through the very same code; this
module re-exports it for the distributed (RpcChannel) callers. Both endpoints
share the same three frame kinds (PARITY_REQ / PARITY_RESP / RECONCILE_DONE), so
E91, distributed BB84, and raw-socket BB84 all reconcile through one module.
"""

from __future__ import annotations

from qne.reconcile import (          # noqa: F401  (re-export)
    bits_to_int,
    drive_cascade,
    secure_key_bits,
    serve_parities,
)

__all__ = ["bits_to_int", "drive_cascade", "secure_key_bits", "serve_parities"]
