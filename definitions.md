# Definitions

Pinned **before** anything measures anything (WORKPLAN.md §4.1). The headline
number is meaningless until these are settled, and settling them mid-build —
with a half-written harness and a deadline — is how a project ends up arguing
about its own metric instead of reporting it.

Every definition here is a *decision*, not a discovery. Where a reasonable
person could pick the other option, that is noted.

---

## 1. Recovered

> A charge is **recovered** if a subsequent collection attempt on the same
> subscription cycle succeeds within the **7-day recovery window**, measured
> from the timestamp of the original failure.

**Included:**

- A retry that succeeds on any attempt within the window — attempt 2 on day 1 and
  attempt 3 on day 6 both count.
- A charge that succeeds after a re-authentication the customer completed inside
  the window.
- A late authorisation (`failed` → later `authorized`) that settles inside the
  window. Razorpay does produce these and ignoring them would understate every
  arm equally, but understate them nonetheless.

**Excluded from the headline number:**

- Anything settling **after** the 7-day window. Reported separately as
  *late recovery* — see §3.
- A customer who cancels and re-subscribes. That is new business, not recovery,
  and counting it would flatter us.
- A charge that never failed in the first place.

**The judgement call:** the window is a policy choice, not a fact. Seven days
matches our stopping rule, so window and policy agree by construction. A longer
window would raise every arm's recovery rate. Because we report *paired
differences between arms on identical events*, the comparison is robust to this
choice even though the absolute number is not — say so rather than defending 7
as though it were derived.

---

## 2. Gross vs net

Both are reported. **Net leads.**

```
gross_recovered = sum(recovered_paise)
net_recovered   = sum(recovered_paise) - sum(cost_paise)
```

`cost_paise` accumulates every attempt the arm made on that subscription —
including attempts that failed, and including notification sends.

**Why net leads:** gross rewards spraying retries at everything, which is
precisely the behaviour we claim to improve on. An arm can win on gross while
losing money. Net is the number a merchant would actually care about, and it is
the only one under which "fewer wasted attempts" is a real result rather than a
consolation prize.

Both appear in the results table. Leading with net and showing gross beside it
is more honest than picking whichever flatters us.

---

## 3. Attribution and edge cases

| Case | Counts as recovered? | Tag |
|---|---|---|
| Retry succeeds in window | Yes | `retry` |
| Re-auth completed, charge succeeds in window | Yes | `reauth` |
| Late authorisation settles in window | Yes | `late_auth` |
| Customer pays via a link after a notification | Yes | `notify_assisted` |
| Anything settling after the window closes | **No** — reported separately | `late_recovery` |
| Customer cancels, then re-subscribes | No | `new_business` |
| Charge succeeded on first attempt | N/A — never entered the population | — |

Every recovery carries its tag in the ledger. This matters because
`notify_assisted` is the weakest attribution claim we make — the customer may
have paid anyway — so it must be separable from the headline on request rather
than quietly folded in.

---

## 4. Wasted attempt

> An attempt is **wasted** if it was executed and failed, on a subscription that
> was **never recovered within the window**.

An attempt that fails but is followed by a successful one is *not* wasted — it
was part of a sequence that worked. This definition deliberately can only be
evaluated retrospectively, once the window has closed. That is correct: whether
effort was wasted is not knowable at the time it is spent.

Reported as a count and as `attempts_per_recovery`, which is the more legible of
the two.

---

## 5. The population

The denominator is **failed charge attempts**, not subscriptions and not
customers. One subscription failing in three separate cycles contributes three
events.

`recovery_rate = recovered_events / failed_events`

Fixed across all arms by construction, since paired replay gives every arm the
identical event set.

---

## 6. Compliance violation

> Any executed action that a correctly-implemented compliance gate would have
> blocked.

Target is **zero**, and zero is the only acceptable result. This is not a metric
we optimise — it is an invariant we assert. A non-zero count is a bug, and it is
reported as a bug rather than as a trade-off against recovery.

Counted separately and never netted against revenue: "we recovered more but
violated three rules" is not a trade we are willing to present.

---

## 7. Money units

Integer **paise** everywhere, matching Razorpay's API and
[models.py](recovery/models.py). Floats are for probabilities and expected
values only, never for a booked amount. Display converts to rupees at the very
edge — nothing upstream of the UI knows what a rupee is.
