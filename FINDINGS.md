# Findings

What the measurement actually shows — including the parts that contradict what
this project set out to demonstrate.

---

## 1. Headline

30 seeds × 3 cycles × 2,000 events, paired replay with common random numbers,
95% CI on the paired difference. Runtime 114s.

| arm | net | gross | recovery | attempts | wasted |
|---|---|---|---|---|---|
| naive (1 retry) | ₹511,419 | ₹650,381 | 14.9% | 2,213 | 1,644 |
| fixed 3× (72h apart) | ₹886,153 | ₹1,063,424 | 24.7% | 3,581 | 2,115 |
| agent, retry-only | ₹1,297,608 | ₹1,988,384 | 30.8% | 5,109 | 3,275 |
| **agent, full** | **₹1,365,813** | ₹2,125,298 | **33.1%** | 5,400 | 3,440 |

**agent vs fixed-3×: +₹479,660 per run [+361,762, +597,557], = +54.1%**
Compliance violations: **0**.

---

## 2. The advantage is NOT from smarter retry timing

This is the finding that matters most, and it contradicts the premise the
project was built on.

The whole design rests on the idea that *when* you retry matters — retry
insufficient funds after payday, retry a bank outage in two hours. So the
sensitivity sweep flattened the delay curve, making timing worthless, expecting
the advantage to collapse.

It went **up**:

| delay sensitivity | agent vs fixed-3× |
|---|---|
| 0.0 — timing worthless | **+133.5%** |
| 1.0 — timing as configured | +33.9% |

15 seeds, both significant. The agent wins *more* when timing stops mattering.

**What is actually driving the result**, then, is cause-aware triage:

- it refuses to retry dead mandates and hard declines, where the fixed schedule
  burns three attempts each;
- it reaches expired cards and authentication failures through re-auth, which a
  retry-only schedule can never recover at all.

And the reason the gap *narrows* when timing matters is unflattering in the
other direction: the fixed baseline retries at 72h, which happens to sit near
the good zone for insufficient funds. Give timing real weight and the dumb
schedule gets luckier.

**The claim must change** from "we retry at smarter times" to "we work out which
failures are worth pursuing, and how". That is closer to the track's actual
brief — *determines the right intervention* — but it is not what the workplan
said, and the write-up must not keep the old story.

---

## 3. The "fewer wasted attempts" claim is false

The workplan promised *"Z% fewer wasted retry attempts"*. The data says the
opposite:

| arm | attempts | wasted |
|---|---|---|
| fixed 3× | 3,581 | 2,115 |
| agent | 5,400 | 3,440 |

The agent does **more** work, not less, and wastes more in absolute terms. It
wins on money recovered, not on efficiency. Every version of the efficiency
claim has to come out.

The one defensible efficiency statement is per-recovery: the agent spends
~5,400 attempts for 33.1% recovery against 3,581 for 24.7%, so ~163 attempts
per point of recovery versus ~145. It is still **worse** on that measure.

---

## 4. Where the advantage stops holding

From the sweep (6 seeds/point — indicative, not precise):

| parameter | result |
|---|---|
| delay sensitivity | holds across 0.0–1.0 (and is *stronger* at 0.0, §2) |
| attempt cost | holds from ₹0 to ₹20 per attempt |
| **base recovery rate × 0.5** | **agent LOSES by 58%** |
| **LTV = 36 months** | **agent LOSES by 149%** |
| churn penalty off | +229.7% |

Two genuine failure modes, and both should be stated rather than hidden:

**In a harder world the agent loses.** Halve every recovery rate and the fixed
schedule beats it. The agent stops early when expected value goes negative; the
dumb schedule keeps trying and occasionally gets lucky. Caution is the right
policy on average and the wrong policy when everything is marginal.

**With a long customer lifetime the agent stops acting.** At 36 months the churn
penalty dominates every action and the agent does almost nothing. This is the
softest parameter in the model (`sources.md` §3) driving the largest swing, and
it is the strongest argument for reporting the churn-off number alongside every
headline.

---

## 5. Why the uplift sits above the published band

Published smart-retry uplift over fixed schedules is **15–40%**
(`sources.md` §2). Ours is +54.1% full, +46.4% retry-only. The plausibility
check in the harness flags this automatically rather than letting it pass.

The most likely explanation follows from §2: the published band measures retry
*timing* against retry timing, and our advantage is not timing — it is triage.
We are measuring a broader intervention against a narrower benchmark, so the
comparison is not like-for-like and the number should not be quoted as though
it were.

The honest framing: **"+54% against a fixed-3× baseline in our simulation,
which is above the published 15–40% band for smart retries — most likely because
our agent also declines unrecoverable cases and uses re-authentication, which
that benchmark does not cover."**

---

## 6. What is still not verified

- **RBI thresholds** are checked against secondary reporting, not the circular
  text. The 24h pre-debit notice and ₹15,000 AFA threshold are confirmed; the
  ₹1,00,000 category exemption and the post-debit notification requirement were
  found late and the latter is modelled but not enforced end to end.
- **Churn probabilities are calibrated, not cited** (`sources.md` §3). They were
  set so behaviour lands in the published recovery band. That is an external
  target rather than an invented one, but it is still calibration.
- **The simulator is ours.** Every number above describes a world we built from
  cited benchmarks. §4 is the honest answer to how much that matters.
