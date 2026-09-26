"""Regression tests for the BMv2 loss-threshold verifier.

`set_channel_loss` writes the fiber-loss threshold into the running switch and then
re-reads `table_dump` to confirm it landed — without that check a silently-rejected
write leaves every photon dropped or the loss frozen at the previous distance.

The parsing of that dump has been wrong twice, in opposite directions, and each
failure cost a whole sweep:
  * `int(token, 0)` raised ValueError on a leading zero ('01'), so every scenario
    point aborted before it was measured;
  * reading unprefixed tokens as decimal discarded genuine hex like '0b859b1b';
  * reading everything as hex broke the decimal form that
    tests/test_validation.py::TestFabricLossUpdates pins as real CLI output.

The rule the dump actually follows: 0x-prefixed is hex, all-digits is decimal, and
an unprefixed token containing a-f is hex because it cannot be decimal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from deploy_fabric import threshold_in_dump  # noqa: E402


def dump(action_data: str) -> str:
    return (
        "Dumping entry 0x0\n"
        "Match key:\n"
        "* wavelength          : EXACT     00\n"
        f"Action entry: set_channel_params - {action_data}\n"
    )


def test_unprefixed_hex_threshold_is_found():
    """0b859b1b is not a decimal literal; reading it as decimal discarded it."""
    assert threshold_in_dump(dump("0b859b1b, 01, 0669516a6a14, 066a6a14d600"), 0x0B859B1B)
    assert threshold_in_dump(dump("0b859b1b, 01, 0669516a6a14, 066a6a14d600"), 193305371)


def test_leading_zero_token_does_not_raise():
    """int(token, 0) raised on '01' and took the whole sweep down with it."""
    d = dump("01977450, 01, 0669516a6a14, 066a6a14d600")
    assert threshold_in_dump(d, 1977450) is True        # decimal per the documented rule
    assert threshold_in_dump(d, 999) is False           # and no exception either way


def test_decimal_action_data_is_accepted():
    """The form TestFabricLossUpdates pins: bare decimal, no prefix."""
    assert threshold_in_dump(dump("193273528, 1"), 193273528)
    assert not threshold_in_dump(dump("193273528, 1"), 0x193273528)


@pytest.mark.parametrize("threshold", [0, 1, 0xFF, 0xDEADBEEF, 2**32 - 1])
def test_roundtrip_for_representative_thresholds(threshold):
    """Whatever set_channel_loss wrote, both dump forms of it must verify."""
    assert threshold_in_dump(dump(f"0x{threshold:x}, 0x1, aabbccddeeff"), threshold)
    assert threshold_in_dump(dump(f"{threshold}, 1, aabbccddeeff"), threshold)


def test_mismatched_threshold_is_rejected():
    assert not threshold_in_dump(dump("0b859b1b, 01, aabbccddeeff, 112233445566"), 0x0B859B1C)


def test_prefixed_hex_is_accepted_too():
    assert threshold_in_dump(dump("0x0b859b1b, 0x01, aabbccddeeff"), 0x0B859B1B)


def test_only_the_action_line_is_searched():
    """A match key or another table's entry must not satisfy the check."""
    d = ("Dumping entry 0x0\n"
         "Match key:\n"
         "* wavelength          : EXACT     0b859b1b\n"
         "Action entry: other_action - 00000000\n")
    assert not threshold_in_dump(d, 0x0B859B1B)


def test_empty_dump_is_rejected():
    assert not threshold_in_dump("", 12345)
    assert not threshold_in_dump("Dumping entry 0x0\n(nothing)\n", 12345)
