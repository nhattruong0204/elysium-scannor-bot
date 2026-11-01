"""Contract-address string extraction. Extraction is NOT verification (BLOCKCHAIN_RESEARCH §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from Crypto.Hash import keccak

from elysium_bot.db.enums import AddressFormat, ChecksumState
from elysium_bot.extraction.links import URL_RE

EVM_RE = re.compile(r"(?<![0-9a-zA-Z])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_B58 = "1-9A-HJ-NP-Za-km-z"
BASE58_RE = re.compile(rf"(?<![{_B58}])[{_B58}]{{32,44}}(?![{_B58}])")


@dataclass(frozen=True)
class ContractString:
    raw: str
    normalized: str
    format: AddressFormat
    checksum_state: ChecksumState
    position: int


def eip55_checksum(address: str) -> str:
    """Return the EIP-55 mixed-case form of a 0x-prefixed hex address."""
    hex_part = address[2:].lower()
    digest = keccak.new(digest_bits=256, data=hex_part.encode("ascii")).hexdigest()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(hex_part)
    )


def _checksum_state(raw: str) -> ChecksumState:
    body = raw[2:]
    if body == body.lower() or body == body.upper():
        return ChecksumState.ALL_LOWER_OR_UPPER
    if eip55_checksum(raw) == raw:
        return ChecksumState.VALID_CHECKSUM
    return ChecksumState.INVALID_CHECKSUM


def _looks_like_base58_address(token: str) -> bool:
    return (
        any(c.isdigit() for c in token)
        and any(c.isupper() for c in token)
        and any(c.islower() for c in token)
    )


def extract_contracts(text: str) -> list[ContractString]:
    hits: list[ContractString] = []
    for m in EVM_RE.finditer(text):
        raw = m.group(0)
        hits.append(
            ContractString(raw, raw.lower(), AddressFormat.EVM, _checksum_state(raw), m.start())
        )

    # Base58-like strings (e.g. Solana mints): ignore anything inside URLs or EVM matches.
    masked = EVM_RE.sub(lambda m: " " * len(m.group(0)), text)
    masked = URL_RE.sub(lambda m: " " * len(m.group(0)), masked)
    for m in BASE58_RE.finditer(masked):
        token = m.group(0)
        if _looks_like_base58_address(token):
            hits.append(
                ContractString(
                    token,
                    token,
                    AddressFormat.BASE58_LIKE,
                    ChecksumState.NOT_APPLICABLE,
                    m.start(),
                )
            )
    hits.sort(key=lambda h: h.position)
    return hits
