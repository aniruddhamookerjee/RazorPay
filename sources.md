# Sources

Every number that drives a simulated outcome gets a citation **here, as it is
written into the code** — not retro-fitted before the deadline.

This file is the single defence against the sharpest question this project can
be asked: *"you invented the recovery rates, your agent discovered them, so of
course it beat the baseline."* An entry with a real link is evidence. An entry
marked `ASSUMPTION` is an honest admission and a pointer to §4.3's sensitivity
sweep. Both are acceptable. A number with neither is not.

**Rule:** if a parameter reaches the generator or the priors without a row in
this table, it is a bug.

---

## 1. Failure cause distribution

What fraction of failed recurring charges fall into each cause. Drives the
generator's event mix.

| Cause | Value | Source | Status |
|---|---|---|---|
| `insufficient_funds` | TBD | | ☐ |
| `card_expired` | TBD | | ☐ |
| `mandate_expired` | TBD | | ☐ |
| `mandate_revoked` | TBD | | ☐ |
| `bank_unavailable` | TBD | | ☐ |
| `network_timeout` | TBD | | ☐ |
| `hard_decline` | TBD | | ☐ |
| `authentication_required` | TBD | | ☐ |

*Where to look:* published dunning/involuntary-churn reports from subscription
billing vendors; card-network decline-code taxonomies for the soft/hard split.

**Reason vocabulary is settled** — see §7.

---

## 2. Recovery probability by cause and timing

The load-bearing numbers. `p(success | cause, attempt #, delay)` — the priors
the Beta-Binomial model starts from before observing anything.

| Cause | Attempt | Delay | p(success) | Source | Status |
|---|---|---|---|---|---|
| `insufficient_funds` | 2 | +24h | TBD | | ☐ |
| `insufficient_funds` | 2 | +3d | TBD | | ☐ |
| `insufficient_funds` | 2 | post-salary window | TBD | | ☐ |
| `bank_unavailable` | 2 | +2h | TBD | | ☐ |
| `network_timeout` | 2 | +1h | TBD | | ☐ |
| `card_expired` | 2 | any | TBD | | ☐ |
| `mandate_expired` | 2 | any | TBD (expect ~0) | | ☐ |
| `hard_decline` | 2 | any | TBD (expect ~0) | | ☐ |

**Held out from the agent.** These parameters live in the generator's config and
are never readable by the policy — it must reach its own estimates from observed
outcomes. Enforced by module boundary, and worth a test.

---

## 3. Costs

| Parameter | Value | Source | Status |
|---|---|---|---|
| Per-attempt gateway/bank fee | TBD | | ☐ |
| SMS notification cost | TBD | | ☐ |
| WhatsApp notification cost | TBD | | ☐ |
| Customer LTV (for churn penalty) | TBD | | ☐ |
| `P(churn \| n failed attempts)` | TBD | | `ASSUMPTION` |

**`P(churn | attempts)` is the softest number in the model** and the one doing
the most work — it is what makes the agent stop early, which produces the
"fewer wasted attempts" result. It gets its own sensitivity sweep, and results
are reported at **churn penalty = 0** as well, so the agent's advantage can be
seen with this assumption switched off entirely (WORKPLAN.md §4.3).

---

## 4. Segment effects

Planted patterns the detector should find on the 10k batch — and must *not*
find on the null batch.

| Segment | Effect | Source | Status |
|---|---|---|---|
| Bank X, high-value charges | TBD | | `ASSUMPTION` |
| Time-of-day window | TBD | | `ASSUMPTION` |

These are synthetic by construction: the point is testing whether the detector
recovers a known signal and stays silent on noise, not claiming a real bank
behaves this way. Never presented as a finding about any real institution.

---

## 5. Compliance rules

Not simulation parameters — hard constraints. Cited because a judge may check
them, and because getting them wrong is worse than getting a rate wrong.

| Rule | Value | Source | Status |
|---|---|---|---|
| RBI e-mandate pre-debit notification | 24h before debit | | ☐ |
| AFA threshold for recurring e-mandates | ₹15,000 | | ☐ |
| Per-mandate retry limits | TBD | | ☐ |
| Consent / DND for messaging | TBD | | ☐ |

Verify against the RBI circulars directly rather than secondary summaries —
these have been revised more than once.

---

## 6. Schema provenance

| Fixture | Origin | Status |
|---|---|---|
| `payment.failed` | Razorpay **Payment Links**, test mode — real capture | ☐ pending capture |
| `payment.captured` | Razorpay **Payment Links**, test mode — real capture | ☐ pending capture |
| `subscription.charged` | **Not captured** — see note below | n/a |

If a fixture is doc-derived rather than captured from a live test-mode webhook,
say so **here and in the README**. WORKPLAN.md Day 1 permits the substitution;
it does not permit being quiet about it.

### Note: why payloads come from Payments, not Subscriptions

Razorpay Subscriptions is a separately-entitled product and was **not enabled on
this test account**. Verified directly, same credentials, same session:

| Endpoint | Status |
|---|---|
| `/v1/payments`, `/v1/orders`, `/v1/invoices`, `/v1/customers`, `/v1/payment_links` | 200 |
| `/v1/plans`, `/v1/subscriptions` | **401** |

Enabling it requires full account activation (KYC), quoted at 3–4 working days
against an 8-day deadline, and would have meant handing over personal bank
details to unlock a path the project does not depend on.

**What this changes:** nothing structural. A failed payment returns the same
`error.code` / `error.reason` / `error.step` fields regardless of whether the
charge originated from a subscription cycle. The payload *shape* is grounded in
a real capture; subscription *semantics* — mandate, cycle, attempt number — are
modelled in `recovery/models.py` and simulated, as they always were for the bulk
of the batch.

**What this genuinely costs:** the two mandate-specific causes
(`mandate_expired`, `mandate_revoked`) have no live capture behind them. They
are simulated from cited priors like every other cause, but unlike the others
their payload shape is unverified. Flag this in the write-up rather than letting
it be discovered.

---

## 7. Error reason vocabulary

**Test mode returns a single generic reason — verified, not assumed.** Three
`payment.failed` payloads were captured on 27 Aug through the Payment Links
flow, using different Razorpay scenario test cards (generic failure, and the
documented *card declined* card `4100 2800 0006 0003`). All three returned
**byte-identical error fields**:

```
error_code         BAD_REQUEST_ERROR
error_reason       payment_failed
error_description  Payment failed
error_source       gateway
error_step         payment_authorization
```

`payment_failed` is a catch-all. Razorpay documents scenario-specific test cards
(insufficient funds, card declined, timeout, authentication failed), but through
the hosted Payment Links page they do **not** yield distinct `error.reason`
values — the differentiation the docs describe did not materialise in three
attempts. The Day 3 taxonomy therefore **cannot** be built from captures.

The consistency across three captures is itself worth something: the envelope
shape is stable and verified, not a single lucky sample.

**Production does discriminate.** Razorpay documents 115+ specific
`error.reason` values, including every cause in the `Cause` enum:
`insufficient_funds`, `card_expired`, `card_declined`, `authentication_failed`,
and mandate-related codes.

- Reference list: <https://razorpay.com/docs/payments/payment-gateway/rainy-day/errors/error-reasons/>
- Downloadable spreadsheet of all reasons:
  <https://razorpay.com/docs/build/browser/assets/images/payments_error_reasons.xlsx>

**So provenance splits cleanly, and nothing is invented:**

| Layer | Source | Status |
|---|---|---|
| Payload envelope + field nesting | Real test-mode capture | ☑ |
| Reason vocabulary (the strings) | Razorpay official docs, above | ☑ |
| Distribution over reasons | Cited benchmarks — §1 | ☐ |
| p(recovery) per reason | Cited benchmarks — §2 | ☐ |

The generator emits **documented** reason strings inside the **captured**
envelope. Say this plainly in the write-up: it is a stronger position than a
capture-only story would have been, because test mode could never have produced
the variety the diagnosis layer exists to handle.

### Segment caveat: `bank` is null on card payments

The captured payload has `bank: None` with `method: card` and
`card.network: Visa`. `bank` appears to populate only for netbanking/UPI. Day 3
Part B segments on bank — so either restrict bank segmentation to non-card
methods, or segment cards on `card.network` / `card.issuer` instead. Decide this
before building the segment matrix, not after it returns empty cells.
