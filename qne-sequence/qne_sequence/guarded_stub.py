"""GuardedRemoteStub — the runtime gate for the `another` seam (DESIGN.md §8.1).

SeQUeNCe's BB84/Cascade are written assuming both protocol objects live in one
address space: they read and *mutate* the peer protocol's state directly through
`self.another` (e.g. ``self.another.key_lengths.append(...)``, ``self.another.set_key()``,
and even ``self.key ^ self.another.key`` — reading the peer's secret key).

In a distributed runtime each process owns exactly one protocol instance, so the
only *legitimate* uses of `another` are addressing:

    another.name        -> the peer protocol's name  (message receiver)
    another.owner.name  -> the peer node's name       (message destination)

We assign a GuardedRemoteStub to `self.another`. It permits exactly those two
reads and raises RemoteAccessError on everything else. Running the full protocol
with the stub installed turns the static "did we convert every access?" question
into a runtime proof: zero RemoteAccessErrors == every cross-process access has
been replaced by a message.
"""

from __future__ import annotations


class RemoteAccessError(AttributeError):
    """Raised when code reaches across the process boundary into the peer protocol.

    Inherits AttributeError so it surfaces clearly, but is a distinct type so the
    timeline / tests can count it specifically rather than swallowing unrelated
    attribute errors.
    """


class _OwnerStub:
    """Stand-in for ``another.owner`` exposing only ``.name``."""

    def __init__(self, node_name: str):
        object.__setattr__(self, "name", node_name)

    def __getattr__(self, item):
        raise RemoteAccessError(
            f"illegal cross-process read: another.owner.{item} "
            f"(only another.owner.name is permitted; convert this to a message)"
        )

    def __setattr__(self, key, value):
        raise RemoteAccessError(
            f"illegal cross-process write: another.owner.{key} = {value!r} "
            f"(convert this to a message)"
        )


class GuardedRemoteStub:
    """Assigned to ``self.another``; permits only ``.name`` and ``.owner.name``.

    Args:
        peer_proto_name: name of the peer's protocol instance (e.g. "bob.BB84").
        peer_node_name:  name of the peer node (e.g. "bob").
    """

    def __init__(self, peer_proto_name: str, peer_node_name: str):
        object.__setattr__(self, "name", peer_proto_name)
        object.__setattr__(self, "owner", _OwnerStub(peer_node_name))

    def __getattr__(self, item):
        raise RemoteAccessError(
            f"illegal cross-process access: another.{item} "
            f"(only another.name and another.owner.name are permitted; "
            f"convert this read/write/call to a message — see DESIGN.md §8.1)"
        )

    def __setattr__(self, key, value):
        raise RemoteAccessError(
            f"illegal cross-process write: another.{key} = {value!r} "
            f"(convert this to a message — see DESIGN.md §8.1)"
        )
