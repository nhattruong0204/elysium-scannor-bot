from __future__ import annotations

import pytest

from elysium_bot.db.enums import AddressFormat, ChecksumState, LinkKind
from elysium_bot.extraction.contracts import eip55_checksum, extract_contracts
from elysium_bot.extraction.links import extract_links
from elysium_bot.extraction.tickers import extract_handles, extract_tickers

# Test vectors from EIP-55.
EIP55_VALID = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


@pytest.mark.parametrize("address", EIP55_VALID)
def test_eip55_vectors(address: str) -> None:
    assert eip55_checksum(address.lower()) == address
    (hit,) = extract_contracts(f"CA: {address}")
    assert hit.checksum_state == ChecksumState.VALID_CHECKSUM
    assert hit.format == AddressFormat.EVM
    assert hit.normalized == address.lower()


def test_flipped_case_is_invalid_checksum() -> None:
    valid = EIP55_VALID[0]
    flipped = valid[:3] + valid[3].swapcase() + valid[4:]
    (hit,) = extract_contracts(flipped)
    assert hit.checksum_state == ChecksumState.INVALID_CHECKSUM


@pytest.mark.parametrize(
    "address",
    ["0x52908400098527886e0f7030069857d2e4169ee7", "0x52908400098527886E0F7030069857D2E4169EE7"],
)
def test_single_case_has_no_checksum_info(address: str) -> None:
    (hit,) = extract_contracts(address)
    assert hit.checksum_state == ChecksumState.ALL_LOWER_OR_UPPER


def test_address_inside_url_and_positions() -> None:
    text = f"see https://elysium.kinetiq.xyz/address/{EIP55_VALID[1]} ok"
    (hit,) = extract_contracts(text)
    assert hit.raw == EIP55_VALID[1]
    assert text[hit.position : hit.position + 42] == hit.raw


@pytest.mark.parametrize(
    "text",
    [
        "0x" + "a" * 41,  # 41 hex chars
        "0x" + "a" * 39,  # too short
        "tx 0x" + "b" * 64,  # tx hash
        "1x" + "a" * 40,
    ],
)
def test_non_addresses_ignored(text: str) -> None:
    assert [h for h in extract_contracts(text) if h.format == AddressFormat.EVM] == []


def test_multiple_and_duplicate_addresses_kept_in_order() -> None:
    a, b = EIP55_VALID[0], EIP55_VALID[1]
    hits = extract_contracts(f"{a} vs {b} and again {a.lower()}")
    assert [h.normalized for h in hits] == [a.lower(), b.lower(), a.lower()]


def test_base58_like_detection_excludes_urls_and_words() -> None:
    sol = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    hits = extract_contracts(f"sol mint {sol}")
    assert [(h.format, h.checksum_state) for h in hits] == [
        (AddressFormat.BASE58_LIKE, ChecksumState.NOT_APPLICABLE)
    ]
    assert extract_contracts("https://example.com/" + sol) == []
    assert extract_contracts("a" * 40) == []


def test_links_from_text_and_entities() -> None:
    text = (
        "Docs: https://docs.example.xyz/intro, chart https://dexscreener.com/hyperevm/0xabc. "
        "Join t.me/elysium_chat"
    )
    entities = [
        {"type": "text_link", "offset": 0, "length": 4, "url": "https://x.com/Enter_Elysium"}
    ]
    links = extract_links(text, entities)
    by_url = {link.url: link for link in links}
    assert by_url["https://docs.example.xyz/intro"].kind == LinkKind.DOCS
    assert by_url["https://dexscreener.com/hyperevm/0xabc"].kind == LinkKind.DEX
    assert by_url["https://t.me/elysium_chat"].kind == LinkKind.TELEGRAM
    assert by_url["https://x.com/Enter_Elysium"].kind == LinkKind.X
    assert by_url["https://x.com/Enter_Elysium"].domain == "x.com"


def test_link_dedup_and_trailing_punctuation() -> None:
    links = extract_links("go to https://kinetiq.xyz/. Again: https://kinetiq.xyz/", None)
    assert [link.url for link in links] == ["https://kinetiq.xyz/"]


def test_idn_domain_is_flagged() -> None:
    (link,) = extract_links("claim at https://xn--kintiq-bva.xyz/airdrop", None)
    assert link.is_idn
    assert link.domain == "xn--kintiq-bva.xyz"
    assert link.display_domain != link.domain


def test_explorer_and_github_kinds() -> None:
    links = extract_links("https://github.com/org/repo https://hyperevmscan.io/token/0x1", None)
    assert [link.kind for link in links] == [LinkKind.GITHUB, LinkKind.EXPLORER]


def test_tickers_and_handles() -> None:
    text = "aping $NEST and $hype, not $5 or US$100. ping @kinetiq_xyz or email a@b.co"
    assert extract_tickers(text) == ["NEST", "HYPE"]
    assert extract_handles(text) == ["kinetiq_xyz"]
