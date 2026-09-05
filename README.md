# AI Revenue Recovery

**A cause-aware agent for failed recurring payments.** It works out *why* each
charge failed, picks the action most likely to recover that specific case,
executes it inside hard compliance limits, and proves the result against two
baselines on identical events.

Built for the Razorpay **AI Revenue Recovery** track.

---

## The problem

A merchant charges ₹499 a month. Razorpay tries to collect it automatically. The
charge fails — not because the customer wants to leave, but for a mechanical
reason: the account was empty *that day*, the card expired, the mandate lapsed,
the bank had a bad hour.

Most systems answer all of those the same way: *retry after 24 hours, once.* That
single rule is wrong in both directions. It **burns attempts** on cases that can
never succeed — retrying a revoked mandate is not merely futile, it is a
compliance problem — and it **gives up** on cases that would have worked with a
different action. The money leaks quietly, because each individual failure is
small.

---

## The result

30 seeds × 3 cycles × 2,000 events. Paired replay: every arm sees identical
events, and the coin deciding whether an action succeeds is hashed from the
decision, so a difference between arms is strategy, never luck.

| arm | net recovered | recovery rate | attempts | wasted |
|---|---:|---:|---:|---:|
| naive — one retry, +24h | ₹507,324 | 14.9% | 2,212 | 1,642 |
| fixed 3× — 72h apart | ₹899,475 | 24.8% | 3,584 | 2,102 |
| agent, retry-only | ₹1,187,633 | 29.3% | 4,982 | 3,236 |
| **agent, full** | **₹1,273,199** | **32.0%** | 5,292 | 3,401 |

**agent vs fixed-3×: +₹373,724 per run, 95% CI [+₹236,254, +₹511,194] — +41.5%**
**Compliance violations: 0.**

**Two caveats belong in the same breath as those numbers.**

*The comparable figure is +32.0%, not +41.5%.* Published smart-retry uplift is
15–40%, and that band measures retry *timing* against retry timing. The full
agent also uses re-authentication and notification channels the baselines never
touch. The **retry-only arm** exists to be like-for-like, and at **+32.0%** it
sits inside the published band.

*The agent uses more attempts, not fewer.* 5,292 against 3,584, and it wastes
more in absolute terms. It wins on money recovered, not on efficiency. An earlier
version of this project claimed otherwise; the data says no.

---

## The batch is simulated. Here is why the result still means something

We cannot fail ten thousand real subscriptions in ten days. If we also invented
the numbers deciding whether a retry succeeds, the agent would be discovering its
own answer key and this would prove nothing. Four defences:

**Sourced, not invented.** Failure distributions and recovery rates come from
published dunning benchmarks, each cited in [`sources.md`](sources.md) with a
link. Where a number is an assumption it is labelled `ASSUMPTION`, not dressed up.

**Held out, and enforced.** The parameters that generate outcomes live in
`recovery/simulation/`, and a test fails the build if any decision-layer module
imports them. The agent's own priors are deliberately *coarser* and **flat over
delay** — it has to learn retry timing from observed outcomes, and a test asserts
the priors contain no timing preference.

**Real payload shapes.** The generator does not hand-write JSON. It loads a real
captured Razorpay `payment.failed` payload and overrides fields, so the schema
cannot drift from production.

**Sensitivity-tested.** We report where the advantage holds *and where it stops*.
It disappears if recovery rates are 25% below published, and reverses at a
36-month customer lifetime. Both are in [`FINDINGS.md`](FINDINGS.md) §4.

---

## Reproduce it

```bash
git clone https://github.com/aniruddhamookerjee/RazorPay && cd RazorPay
py -3.13 -m venv .venv
.venv/Scripts/pip.exe install -r requirements.txt
cp .env.example .env        # Razorpay test keys only needed for live execution
```

The headline table, in about two minutes:

```bash
.venv/Scripts/python.exe -m recovery.experiment --seeds 30 --cycles 3 --batch 2000
```

Where the advantage stops holding:

```bash
.venv/Scripts/python.exe -m recovery.experiment.sensitivity
```

Everything is deterministic from the seed — the same command produces the same
numbers on any machine, and a test asserts it.

Other entry points:

| command | what it does |
|---|---|
| `python -m recovery.simulation.generator --split` | 10k volume batch + 100-event live subset |
| `python -m recovery.experiment.evaluate` | classifier accuracy against ground truth |
| `python -m recovery.narration` | all 18 message templates, for human review |
| `python -m recovery.experiment.export` | freeze a run into dashboard data |
| `python -m scripts.create_payment_link` | create a real test-mode charge |

`pytest -q` runs 141 tests in about 15 seconds.

---

## Architecture

Five layers. A failed charge enters at the top and leaves as a ledger row with
money attached.

| layer | what it does | where |
|---|---|---|
| **1 · Event** | Real Razorpay webhook capture; seeded generator producing 10k events inside a *real* captured envelope; virtual clock replaying a 7-day window in milliseconds | [`webhook.py`](recovery/webhook.py), [`simulation/`](recovery/simulation), [`clock.py`](recovery/clock.py) |
| **2 · Diagnosis** | 98 documented Razorpay reason strings mapped to 9 causes; a local open-source model only for strings the table has never seen; segment patterns tested with Benjamini-Hochberg correction | [`diagnosis/`](recovery/diagnosis) |
| **3 · Decision** | Beta-Binomial success model learning from observed outcomes; cost model including churn risk; compliance gate; EV policy scoring *every* action | [`decision/`](recovery/decision) |
| **4 · Execution** | Simulated and live backends behind one interface, four independent guards on the live path, idempotency enforced by the database | [`execution/`](recovery/execution) |
| **5 · Measurement** | Four arms, paired replay, 95% CIs, sensitivity sweep | [`experiment/`](recovery/experiment) |

**The model never decides where money goes.** Classification fallback, audit
narration and message writing are model jobs. Retry-or-stop is a deterministic
expected-value calculation that can be replayed from the ledger. That is a design
choice, not a limitation — and it is the answer to "is this just a model
guessing?"

### Why it is not a lookup table

A cause→action map is table stakes; Razorpay already ships smart retries. The
policy scores every available action:

```
EV(action) = p_success × amount − attempt_cost − P(churn | attempts) × LTV
```

The intuitive answers fall out of the arithmetic rather than being hardcoded. A
dead mandate has p ≈ 0 on retry, so re-authentication outscores it unaided. A
hard decline drives every attempt negative, so `stop` wins on its own. `stop`
always scores exactly zero and is never blocked, so an action is taken only when
it beats doing nothing.

### Bounded, and provable

The compliance gate runs **before** anything is scored, so a forbidden action is
never ranked — no expected value is large enough to buy past it. Across one
500-event batch it refused **8,713 actions**:

| rule | blocked |
|---|---:|
| quiet hours | 3,500 |
| pre-debit notice not sent | 2,000 |
| max contacts reached | 1,840 |
| pre-debit notice too recent | 728 |
| no active mandate | 450 |
| AFA required above threshold | 195 |

Every one is written to an append-only ledger with its reason. Append-only is
enforced by SQLite triggers, not by convention: `UPDATE` and `DELETE` are refused
at the database.

RBI e-mandate rules are implemented and checked against published reporting: the
24-hour pre-debit notice, the ₹15,000 AFA threshold, and the ₹1,00,000 exemption
for mutual funds, insurance and credit-card bills. Doing that verification found
two gaps in our own first implementation — both recorded in
[`sources.md`](sources.md) §5.

---

## What we got wrong

The most interesting output of this project is not the money figure.

**This system was built on the premise that smarter retry *timing* recovers
money.** The sensitivity sweep tested it by flattening the delay curve until
timing was worthless. The advantage should have collapsed. It rose, monotonically:

| delay sensitivity | agent vs fixed-3× |
|---|---:|
| 0.0 — timing worthless | **+121.0%** |
| 0.5 | +88.5% |
| 1.0 — as configured | +29.5% |

The gain comes from **triage**, not timing: declining dead mandates and hard
declines where a fixed schedule burns three attempts each, and reaching expired
cards through re-authentication, which a retry-only schedule can never recover.
The lead *shrinks* as timing matters more, because the fixed baseline's 72h
happens to sit near the good zone for insufficient funds.

**Every number in this file was also re-measured once.** `event_id` defaulted to
`uuid4()`, and the common-random-numbers hash keys on it — so every replay drew
different coins and nothing was reproducible, while every content-level test
passed. Paired replay was not paired. Fixing it moved the headline from +54.1% to
+41.5%. A test suite that checks content does not check identity.

Both are written up in [`FINDINGS.md`](FINDINGS.md).

---

## Limitations

- **The agent loses in a harder world.** Cut recovery rates 25% below published
  and the edge is gone; halve them and the fixed schedule wins outright. The
  agent stops when expected value goes negative; a dumb schedule keeps trying and
  occasionally gets lucky.
- **Churn probabilities are calibrated, not cited.** They were set so behaviour
  lands in the published recovery band — an external target, but still
  calibration. Every headline is also reported with the churn penalty switched
  off entirely.
- **Razorpay Subscriptions was unavailable** on this account (`/v1/plans` → 401,
  requires full KYC). Schema capture and live execution run through the Payments
  and Payment Links APIs instead. The `error.*` fields are identical, which is
  all the diagnosis layer reads, but the mandate-specific causes have no live
  capture behind them.
- **Post-debit notification is modelled but not enforced end to end.**
- **Hinglish is serviceable, not polished.** Zero-shot generation returned plain
  English; few-shot fixed it, and a checker rejects output that is English
  wearing a Hinglish label. Hand-written fallbacks are better than the generated
  ones and are what ship when the model is unavailable.

---

## Documents

| file | contents |
|---|---|
| [`FINDINGS.md`](FINDINGS.md) | what the measurement shows, including what contradicts the premise |
| [`sources.md`](sources.md) | every parameter with its citation, or an honest `ASSUMPTION` |
| [`definitions.md`](definitions.md) | what "recovered" means — pinned before anything measured it |
| [`WORKPLAN.md`](WORKPLAN.md) | the plan, with superseded claims struck through rather than deleted |

Stack: Python 3.13, FastAPI, SQLAlchemy + SQLite, scipy, Pydantic v2, and
`qwen2.5:7b-instruct` served locally through Ollama — open weights, no API key,
no data leaving the machine.
