# Component: `payment`

**Status:** DRAFT (component extract from master design)
**Master doc:** [`~/.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md`](../../../../.gstack/projects/bigbowlz-ai-audio-experience-social-platform/wanlizhou-main-design-20260413-182237.md) — canonical source.
**Reviewed:** 2026-04-13 (spec review 6/10, red-team)

## Purpose

On-chain agentic payment. The Producer initiates a 0.10 USDC transfer on Base Sepolia from a Producer-owned wallet to the external agent's wallet, on the user's behalf. The tx is real (Basescan-verifiable) and the beat is the exec-panel money shot per master P10.

**The narrative contract:** "an AI agent spent money on behalf of its user on a public chain." Not "a user bought a subscription." The former is the agent economy thesis; the latter is Patreon.

## Key premises

- **P10** Agentic payment: Producer-initiated, user not in the loop, real tx, mock-tx fallback for flake

## Interface contract

```python
class PaymentClient:
    def initiate_tx(
        from_wallet: Wallet,
        to_address: str,
        amount_usdc: float,
        *,
        confirmation_timeout_sec: float = 10.0,
        soft_timeout_sec: float = 5.0,
        mode: Literal["live", "mock"] = "live",
    ) -> PaymentResult:
        """
        live mode:
          - Signs + submits tx via viem on Base Sepolia
          - At soft_timeout_sec: emit `payment.pending` SSE with tx_hash + Basescan URL,
            return PaymentResult(status='pending', tx_hash=..., basescan_url=...)
            → Producer continues to external pitch without waiting for confirmation
          - At confirmation_timeout_sec: if still unconfirmed, emit `payment.mocked`
            and fall back to mock path
        mock mode:
          - Returns a frozen historical tx_hash (hard-coded, pre-captured from a real
            rehearsal tx that still resolves on Basescan), emits `payment.mocked`,
            never touches the chain
        """
```

**PaymentResult shape:**
```python
PaymentResult = {
  "status": "confirmed" | "pending" | "mocked" | "failed",
  "tx_hash": str,
  "basescan_url": str,
  "amount_usdc": float,
  "mode_badge": "LIVE" | "REPLAY",    # UI reads this directly
  "reason": str | None,                # for 'mocked' or 'failed'
}
```

## Dependencies on other components

| Component | Contract | Direction |
|---|---|---|
| `producer` | Producer calls `initiate_tx()` between internal pitch round and external pitch | in |
| `agents` | `alices_agent.wallet_address` is the `to_address` | reads |
| `api-storage` | emits `payment.initiated` / `payment.confirmed` / `payment.pending` / `payment.mocked` SSE | out |

## Build plan touchpoints

- **Day 0 (must happen before Day 1 code):** Spike. Pre-provision a Producer wallet on Base Sepolia. Get test USDC from a public faucet. Verify viem can send 0.10 USDC from Producer wallet to Alice's wallet (a second address you control) in under 30 sec. ~45 min. This is a hard gate — no downstream code if this doesn't work.
- **Day 4:** Wire `initiate_tx()` into Producer's external-invocation flow. Add `payment.*` SSE events. Add LIVE/REPLAY mode-badge UI element. Test both `mode="live"` and `mode="mock"` end-to-end. Pre-capture a frozen tx_hash from one real rehearsal tx for the mock path.
- **Day 6 rehearsal:** Run 2 full end-to-end rehearsals with `mode="live"`. Record any Base Sepolia RPC stalls in a rehearsal log.

## Success criteria

- Day 0 spike: real 0.10 USDC tx confirms in <30 sec on Base Sepolia
- Day 4: both paths (live / mocked) produce visibly different UI (LIVE vs REPLAY badge)
- Day 4: soft-timeout at 5s proceeds Producer to external pitch without blocking
- Day 6: 2/2 rehearsal payment events confirm in <10 sec p50
- Demo day: tx confirms on Basescan within the 48-hour judge-verification window

## Reviewer concerns

### 1. Mock-tx cross-contamination (severity: CRITICAL) — B-2

After a rehearsal enables `mode="mock"` (Base Sepolia was slow), the env var sticks and demo day narrates "AI agent just spent money" over a frozen historical tx hash. An exec clicks Basescan post-demo, sees a stale date, the money shot becomes a legitimacy problem.

**Fix (Day 4, hard requirement):**
- Single env var `PAYMENT_MODE=live|mock` controls all tx routing
- UI header renders a visible badge bound to the exact same env var:
  - `PAYMENT_MODE=live` → green `LIVE tx` badge in the payment event card
  - `PAYMENT_MODE=mock` → amber `REPLAY tx` badge
- You cannot cross the streams without seeing it on screen
- Pre-demo checklist Day 6 & Day 7: verify `PAYMENT_MODE=live` in the actual laptop env before every rehearsal and demo day

### 2. No viem timeout + missing `payment.mocked` SSE event (severity: CRITICAL) — A-Completeness, B-2

Master says "Mock-tx fallback for demo-day flake" but doesn't specify when it fires or what event the UI receives.

**Fix (Day 4, hard requirement):**
- 5s soft-timeout: emit `payment.pending {tx_hash, basescan_url}`. Producer continues to external pitch. Tx still lands in background; UI updates badge to "confirmed" when it does (or stays "pending" through demo if Sepolia is very slow).
- 10s hard-timeout: emit `payment.mocked {frozen_tx_hash, basescan_url, reason: "timeout"}`. Switch badge to REPLAY. Producer continues.
- External pitch proceeds **unconditionally** after soft-timeout. Do not block on payment confirmation.

### 3. Base Sepolia confirmation timing (severity: medium) — A-Feasibility

Master budgets 2-4s; realistic p50 is 4-8s (Sepolia blocks are 2s but mempool + confirmation + eth_getTransactionReceipt polling usually lands 4-8s).

**Fix:** budget 6s; 5s soft-timeout; 10s hard-timeout. Document this timing in README and rehearsal log.

### 4. Single RPC is a SPOF (severity: medium) — B-2

Public Base Sepolia RPC can 429 or stall.

**Fix (Day 0 or Day 4):** configure viem with a second fallback RPC (Alchemy or Ankr free tier). Primary + one fallback. No third tier needed for hackathon scope.

### 5. Deterministic Basescan link for mock path (severity: low, doc-clarity)

The frozen tx_hash for mock mode must resolve on Basescan every time. Pre-capture it from a real Day 4 rehearsal tx, confirm it resolves, then hard-code. Document which tx_hash and which rehearsal captured it.

## Open questions (component-scoped)

- **Wallet funding strategy:** Producer wallet needs test USDC to make 15-30 rehearsal txs. Faucets typically give 10-100 test USDC/day. Check on Day 0 that you can top up the Producer wallet easily mid-week.
- **Gas in ETH:** txs need gas. Base Sepolia ETH from a separate faucet. Pre-fund on Day 0.
- **Alice's wallet:** v0 uses a second address you control (treating it as Alice's). That's fine for demo. Document honestly.
