"""Mock payment.initiate_tx for v0 demo.

Returns deterministic mock TxResult dicts. Honest about being a mock —
`mode = "MOCK"`, tx_hash is `0xMOCK<hash>`, basescan_url is empty.

Real on-chain tx via viem on Base Sepolia is its own follow-up; this
unblocks the Producer external-flow plumbing in Phase 2 of the
2026-04-17 alignment plan (decision 1d).
"""
from __future__ import annotations

import hashlib
from typing import TypedDict


class TxResult(TypedDict):
    mode: str              # "MOCK" | "LIVE" — wire format spec'd by master design
    tx_hash: str           # 0x-prefixed; "0xMOCK" prefix when mode == "MOCK"
    basescan_url: str      # "" for mock; populated when real
    amount_usdc: float
    from_wallet: str
    to_wallet: str


def initiate_tx(from_wallet: str, to_wallet: str, amount_usdc: float) -> TxResult:
    """Stub: returns a deterministic mock TxResult.

    Hash is sha256(from|to|amount) so the same inputs produce the same hash —
    keeps test fixtures stable. NOT cryptographically meaningful.
    """
    raw = f"{from_wallet}|{to_wallet}|{amount_usdc}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return TxResult(
        mode="MOCK",
        tx_hash=f"0xMOCK{digest}",
        basescan_url="",
        amount_usdc=amount_usdc,
        from_wallet=from_wallet,
        to_wallet=to_wallet,
    )
