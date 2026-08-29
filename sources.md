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

Implemented in `recovery/simulation/params.py` as `CAUSE_WEIGHTS`.

| Cause | Value | Source | Status |
|---|---|---|---|
| `insufficient_funds` | 0.45 | "nearly half of all failures" — [Baremetrics](https://baremetrics.com/blog/recover-failed-payments-save-lost-revenue), [Digital Applied](https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026) | ☑ |
| `card_expired` | 0.15 | named as a primary decline category, same sources | ◐ split assumed |
| `hard_decline` | 0.12 | same | ◐ split assumed |
| `authentication_required` | 0.10 | same | ◐ split assumed |
| `bank_unavailable` | 0.07 | "processor error" category, same | ◐ split assumed |
| `network_timeout` | 0.05 | same | ◐ split assumed |
| `mandate_expired` | 0.04 | India-specific; no published split found | `ASSUMPTION` |
| `mandate_revoked` | 0.02 | India-specific; no published split found | `ASSUMPTION` |
| overall failure rate | 0.15 | card failure rate "near 15%" — [Baremetrics](https://baremetrics.com/blog/involuntary-churn) | ☑ |

**Honest reading:** only the insufficient-funds share and the overall failure
rate are properly sourced. The split across the remaining 55% is an assumption
consistent with the categories those sources name but not with numbers they
publish. It is swept.

*Where to look:* published dunning/involuntary-churn reports from subscription
billing vendors; card-network decline-code taxonomies for the soft/hard split.

**Reason vocabulary is settled** — see §7.

---

## 2. Recovery probability by cause and timing

The load-bearing numbers. `p(success | cause, attempt #, delay)` — the priors
the Beta-Binomial model starts from before observing anything.

Implemented as `RECOVERY` in `recovery/simulation/params.py`: a per-cause base
rate times a delay multiplier, times an attempt-decay factor.

| Cause | Base | Delay shape | Source | Status |
|---|---|---|---|---|
| `insufficient_funds` | 0.50 | peaks at 3–5 days | 40–60% band; "spaced 3-5 days apart to align with payday cycles" — [Digital Applied](https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026), [GR4VY](https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/) | ☑ |
| `card_expired` | 0.08 | flat | 50–70% band is for *recovery*, which comes via card update, not retry — retry alone is near-useless | ◐ interpreted |
| `bank_unavailable` | 0.75 | peaks at ~2h | transient; "24h rather than 2h improved recovery by 6.5%" implies short delays matter — [GR4VY](https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/) | ◐ interpreted |
| `network_timeout` | 0.80 | peaks at 1–2h | as above; may surface as late authorisation | ◐ interpreted |
| `hard_decline` | 0.04 | flat | bottom of the 20–40% fraud-hold band | ◐ interpreted |
| `authentication_required` | 0.12 | flat | needs customer action; retry alone rarely helps | `ASSUMPTION` |
| `mandate_expired` / `mandate_revoked` | 0.00 | flat | structurally impossible — no valid mandate, no debit | ☑ by construction |

**Day-of-week and payday effects** (`WEEKDAY_MULTIPLIER`, `PAYDAY_MULTIPLIER`):
"Tuesday through Thursday typically see the highest approval rates, weekends the
lowest"; "paydays (1st and 15th) drive spikes in approval for insufficient-funds
declines" — [GR4VY](https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/), [Solidgate](https://solidgate.com/blog/smart-retries-for-revenue-recovery/). ☑

**Held out — and enforced, not promised.** These live in `recovery/simulation/`,
and `tests/test_generator.py::test_decision_layer_never_imports_simulation`
fails the build if any decision-layer module imports that package.

### The sanity ceiling

Published smart-retry uplift over fixed schedules is **15–40%**
([Solidgate](https://solidgate.com/blog/smart-retries-for-revenue-recovery/),
[GR4VY](https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/)).
Recorded as `PLAUSIBLE_UPLIFT_BAND` and asserted in the Day 6 harness.

If our agent reports an uplift far outside that band, **suspect a bug before
believing the result** — most likely a parameter leak into the policy, or a
strawman baseline. A result that is too good is evidence against itself.

**Held out from the agent.** These parameters live in the generator's config and
are never readable by the policy — it must reach its own estimates from observed
outcomes. Enforced by module boundary, and worth a test.

---

## 3. Costs

| Parameter | Value | Source | Status |
|---|---|---|---|
| Per-attempt fee | ₹2 (200 paise) | not sourced | `ASSUMPTION` |
| Notification cost | ₹0.25 (25 paise) | not sourced | `ASSUMPTION` |
| Customer LTV | ₹12,000 | not sourced | `ASSUMPTION` |
| `P(churn \| 1/2/3 attempts)` | 0.003 / 0.008 / 0.020 | calibrated, not cited | `ASSUMPTION` |

All four are assumptions and all four are swept. None is dressed up as sourced.

### Why the churn values changed on Day 5

The first values (0.01 / 0.03 / 0.07) made the churn penalty on a customer-contact
action **0.24 × the charge**, which is structurally larger than the ~0.15 × charge
a notification can be expected to recover. So the agent never notified. Because
the RBI pre-debit notice is *sent by* notifying, the notice was never sent, so
every debit stayed blocked, and the full batch recovered **1.4% at a net loss**.

That is not a finding about payment recovery — it is a broken parameter. Published
recovery runs 30–70% (§2), so an agent recovering 1.4% is evidence the assumption
is wrong rather than evidence the world is. The values were set so behaviour lands
in the published range.

**This is calibration against an external benchmark, not a citation, and it must
be described that way.** Choosing parameters so the output looks reasonable is
exactly the circularity this file exists to guard against. The defence is that
the calibration target is *published and external*, the parameters are declared
as assumptions, they carry the heaviest weight in the Day 7 sweep, and every
headline number is also reported with the churn penalty switched off entirely.

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

Implemented in `recovery/decision/compliance.py`, all in one named-constant
block marked UNVERIFIED.

| Rule | Value used | Source | Status |
|---|---|---|---|
| RBI e-mandate pre-debit notification | 24h before debit | secondary sources only | ⚠ **UNVERIFIED** |
| AFA threshold for recurring e-mandates | ₹15,000 | secondary sources only | ⚠ **UNVERIFIED** |
| Quiet hours for messaging | 21:00–08:00 | conservative interpretation of TRAI | ⚠ **UNVERIFIED** |
| Per-mandate retry limits | not implemented | — | ☐ gap |

⚠ **These need a human to check them against RBI circulars before submission.**
They come from secondary sources, not from the circulars read directly, and RBI
has revised the e-mandate framework more than once. Compliance is named in the
track's bar, and a wrong threshold is the error a payments judge spots fastest.

They are deliberately grouped in one constant block so verification is a
five-minute job rather than a hunt through the codebase. The AFA threshold also
reads from `.env` (`AFA_THRESHOLD_INR`), so correcting it needs no code change.

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
