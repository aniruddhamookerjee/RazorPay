# Findings

What the measurement actually shows — including the parts that contradict what
this project set out to demonstrate.

> **All numbers below were re-measured on 30 Aug after a reproducibility bug was
> fixed.** `event_id` was defaulting to `uuid4()`, and the experiment's
> common-random-numbers hash keys on it — so every replay drew different coins
> and no reported figure could be reproduced, while every content-level test
> still passed. Earlier drafts of this file quoted the pre-fix numbers. The
> conclusions survived; the figures moved.

---

## 1. Headline

30 seeds × 3 cycles × 2,000 events, paired replay with common random numbers,
95% CI on the paired difference. Runtime 114s.

| arm | net | gross | recovery | attempts | wasted |
|---|---|---|---|---|---|
| naive (1 retry) | ₹507,324 | ₹646,299 | 14.9% | 2,212 | 1,642 |
| fixed 3× (72h apart) | ₹899,475 | ₹1,076,741 | 24.8% | 3,584 | 2,102 |
| agent, retry-only | ₹1,187,633 | ₹1,867,723 | 29.3% | 4,982 | 3,236 |
| **agent, full** | **₹1,273,199** | ₹2,021,509 | **32.0%** | 5,292 | 3,401 |

**agent vs fixed-3×: +₹373,724 per run [+236,254, +511,194], = +41.5%**
**agent (retry-only) vs fixed-3×: +₹288,158 [+127,548, +448,767], = +32.0%**
Compliance violations: **0**.

The retry-only figure of **+32.0% sits inside the published 15–40% band**, and the
harness's plausibility check now passes rather than flagging. That happened by
fixing a bug, not by tuning anything.

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
| 0.0 — timing worthless | **+121.0%** |
| 0.5 — half | +88.5% |
| 1.0 — timing as configured | +29.5% |

15 seeds, all significant, and cleanly monotonic: **the more timing matters, the
smaller the agent's lead gets.** It wins most in a world where timing is
worthless.

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
| fixed 3× | 3,584 | 2,102 |
| agent | 5,292 | 3,401 |

The agent does **more** work, not less, and wastes more in absolute terms. It
wins on money recovered, not on efficiency. Every version of the efficiency
claim has to come out.

The one defensible efficiency statement is per-recovery: the agent spends
~5,292 attempts for 32.0% recovery against 3,584 for 24.8%, so ~165 attempts per
point of recovery versus ~145. It is still **worse** on that measure.

---

## 4. Where the advantage stops holding

From the sweep (6 seeds/point — indicative, not precise):

| parameter | result |
|---|---|
| delay sensitivity | holds across 0.0–1.0 (and is *stronger* at 0.0, §2) |
| attempt cost | holds from ₹0 to ₹20 per attempt |
| **base recovery rate × 0.75** | **-2.1%, not significant — the edge is gone** |
| **base recovery rate × 0.5** | **agent LOSES by 118%** |
| **LTV = 36 months** | **agent LOSES by 143%** |
| churn penalty off | +234.3% |

Two genuine failure modes, and both should be stated rather than hidden:

**In a harder world the agent loses.** Cut recovery rates by a quarter and the
advantage is already gone; halve them and the fixed schedule beats it outright. The agent stops early when expected value goes negative; the
dumb schedule keeps trying and occasionally gets lucky. Caution is the right
policy on average and the wrong policy when everything is marginal.

**With a long customer lifetime the agent stops acting.** At 36 months the churn
penalty dominates every action and the agent does almost nothing (-142.9%). This is the
softest parameter in the model (`sources.md` §3) driving the largest swing, and
it is the strongest argument for reporting the churn-off number alongside every
headline.

---

## 5. Why the uplift sits above the published band

Published smart-retry uplift over fixed schedules is **15–40%**
(`sources.md` §2). Ours is **+41.5% full, +32.0% retry-only**.

The retry-only arm — the one that is actually like-for-like with the band — now
sits **inside** it, and the harness's plausibility check passes. Before the
reproducibility fix these read +54.1% and +46.4%, and the check was flagging
them. Both moved down once the coins stopped being random.

The most likely explanation follows from §2: the published band measures retry
*timing* against retry timing, and our advantage is not timing — it is triage.
We are measuring a broader intervention against a narrower benchmark, so the
comparison is not like-for-like and the number should not be quoted as though
it were.

The honest framing: **"+32% on a like-for-like retry-only comparison, inside the
published 15–40% band; +41.5% once the agent is also allowed to decline
unrecoverable cases and use re-authentication, which that benchmark does not
cover."**

---

## 6. The Hinglish check, and what it caught

WORKPLAN §3.1 said to validate the local model's Hinglish before relying on it.
Doing so changed the design three times.

**Zero-shot Hinglish does not work.** Asked plainly for Hinglish,
`qwen2.5:7b-instruct` returned fluent plain English every time — zero Hindi
words across three attempts. Few-shot examples plus an explicit list of expected
Hindi markers fixed it: 9–12 markers per message, naturally code-mixed. A
`looks_like_hinglish()` check now rejects any "Hinglish" output that is really
English, because shipping English under a Hinglish label to a room of Hindi
speakers would be worse than shipping English and saying so.

**The model printed an internal enum at a customer.** One message read
*"aapka charge bank_unavailable se fail hua"*. Only human-readable descriptions
reach the prompt now, and any output containing a `Cause` value is discarded.

**The model invented a date.** One message said *"there wasn't enough balance on
the 15th"*. No date exists anywhere in the facts it was given. Any digit outside
the `{amount}` placeholder now disqualifies a template.

**It also gave the wrong instruction.** A customer whose bank was down was told
to go and check their account, when the correct message was that no action was
needed. Automated guards cannot catch this — it needs a person. There are only
18 templates (9 causes × 2 languages), so
`python -m recovery.narration` dumps the whole set for review in one screen.

**Honest assessment of the Hinglish quality:** it is serviceable, not polished.
Phrases like *"thoda problem tha kyunki"* are grammatically rough and a native
speaker would notice. The hand-written fallbacks are better than the generated
ones and are what ships when the model is unavailable.

**Cost.** ~15 seconds per generation. Per-customer messages across 10,000 events
are impossible, so messages are generated as templates per (cause, language,
channel) and the name and amount are filled in locally. The model never handles
a rupee figure.

---

## 7. What is still not verified

- **RBI thresholds** are checked against secondary reporting, not the circular
  text. The 24h pre-debit notice and ₹15,000 AFA threshold are confirmed; the
  ₹1,00,000 category exemption and the post-debit notification requirement were
  found late and the latter is modelled but not enforced end to end.
- **Churn probabilities are calibrated, not cited** (`sources.md` §3). They were
  set so behaviour lands in the published recovery band. That is an external
  target rather than an invented one, but it is still calibration.
- **The simulator is ours.** Every number above describes a world we built from
  cited benchmarks. §4 is the honest answer to how much that matters.
- **Reproducibility was broken until 30 Aug** and nothing caught it: the batch
  content was identical run to run, so every content-level test passed while the
  outcomes were random. It was found by noticing that two exports of the same
  seed disagreed. There is now a test asserting the whole experiment is
  reproducible, but the general lesson stands — a test suite that checks content
  does not check identity.
