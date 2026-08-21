# VIX Trader Bot — Training Manual

A plain-English guide to how the VIX Trader bot works, what it's watching for,
when it trades, when it won't, and how to check up on it. For the
line-by-line technical reference (config names, code paths), see
`VIX_OPERATIONS_GUIDE.md` — this document is the "explain it to a person"
version.

**Broker:** Alpaca, paper trading account (no real money at risk).
**Market data:** Unusual Whales.
**Runs:** automatically, weekdays only, via two scheduled jobs (more in
§8 below).

---

## 1. The big picture

The bot watches the VIX (the market's "fear gauge") and trades three
things in response to it:

| Vehicle | What it is | Strategy |
|---|---|---|
| **SVIX shares** | An ETF that moves *opposite* to VIX — profits when volatility falls/stays calm | The "Ladder" — buys in on spikes, sells into calm |
| **VXX calls** | An option that profits if volatility *rises* | Tactical, calendar-driven |
| **UVXY/VXX puts** | An option that profits if a volatility spike *fades* back down | Tactical, spike-driven |

These are two **completely independent** systems running side by side —
the SVIX Ladder and the Options sleeve. Nothing about one affects the
other's decisions.

---

## 2. The SVIX Ladder (spike-buying strategy)

**In one sentence:** the bot ignores SVIX while things are calm, starts
buying it in chunks once the VIX spikes past 30, keeps buying more as it
spikes higher, and starts selling for profit once the spike starts
cooling off.

### Getting in (arming and buying rungs)
- Nothing happens while VIX is 30 or below.
- The moment VIX prints **above 30**, the campaign "arms" — it's now
  actively watching for buying opportunities.
- Once armed, it buys **$5,000 worth of SVIX** every time VIX crosses a
  new 10-point level: 30, then 40, then 50, then 60, and so on. Each level
  is only bought once per campaign — it won't re-buy the same level if
  VIX retests it.
- It keeps doing this until it's committed **15% of the account's total
  value** to the position — the last purchase is sized down to whatever
  is left in that budget, then buying stops.
- Unlike everything else in the bot, there's no "one trade per day" limit
  here — if VIX rips through 30 → 40 → 50 in a single session, it can buy
  all three rungs that same day.

### Getting out (pullback and taking profit)
- The bot tracks the highest VIX level seen since arming (the "campaign
  peak"). Once VIX drops **3% below that peak**, the campaign considers
  the spike to be cooling off — this stops any further buying and starts
  the profit-taking phase.
- From there, it sells **25% of the largest position it ever held** each
  time profit crosses +25%, +50%, +75%, and +100% (measured against the
  average price paid). The last of these four sales clears out whatever
  is left, so nothing gets stranded as an odd leftover.
- If VIX turns back around and spikes to a *new* high before the position
  is fully sold, buying resumes automatically at the next unbought rung —
  and the profit-taking counter restarts from zero against the new,
  larger position. This prevents the bot from getting "stuck" holding a
  partial position if volatility has a second leg up.
- Once the position is fully sold down to zero, the campaign resets and
  waits for the next VIX crossing above 30 to start over.

### Risk control
There is **no stop-loss** on this position by design — the strategy is
built to ride out some drawdown while scaling in, so a P&L-based stop
would work against the strategy's own logic. The only risk control is the
15%-of-account budget cap described above.

---

## 3. The Options Sleeve — Calls (betting volatility rises)

**When it buys:** the bot buys a VXX call when all of the following are
true:
1. It's currently **August, September, or October** — the calendar
   window this tactical long-volatility bias applies to.
2. No fade-spike put setup is currently active (see §4 — that takes
   priority).
3. No VXX or UVXY option is already held.

**What it buys:** a VXX call expiring **21–45 days out**, choosing the
strike whose delta sits in the 0.40–0.60 range (roughly "40–60% likely to
expire in the money" — a middle-of-the-road, not-too-aggressive
selection). If nothing in that delta range is available, it falls back to
the closest available contract in that expiration window.

**How many contracts:** the bot compares two limits and uses whichever is
smaller:
- **5% of account value**, minus anything already committed to the
  options sleeve, divided by the cost of one contract.
- A flat cap of **5 contracts** per trade (`VIX_MAX_CONTRACTS`), no matter
  how much budget is available.

*Real example from a recent check:* with a ~$399K account and a VXX
$19 call trading around $0.825 (mid of $0.75 bid / $0.90 ask), the 5%
budget would technically allow up to ~242 contracts — but the flat 5-
contract cap kicks in first, so the actual order was 5 contracts, about
$412.50 total. In practice, the flat cap is almost always what limits
the trade size, not the percentage budget.

---

## 4. The Options Sleeve — Puts (betting a spike fades)

**When it buys:** all three of the following must be true:
1. A spike has *already happened* — either UVXY is up 30%+ over the last
   10 trading sessions, **or** VIX has jumped to 25+ from a calmer
   starting point below 20.
2. The most recent close is a "lower high" — i.e., there's early evidence
   the spike is starting to fade, not still accelerating.
3. Nothing is currently held in this position already.

If confirming data isn't available, the bot deliberately does **not**
guess — it skips the trade rather than risk buying into an unconfirmed
setup.

**What it buys:** a put roughly **10% out-of-the-money**, expiring
**10–21 days out**. UVXY is the primary choice; it falls back to VXX if
UVXY's order book is too thin or the spread too wide.

---

## 5. Taking profit or cutting losses on options

Once a call or put is held, it's managed every cycle purely by its
profit/loss, completely independent of what the entry rules say right
now:

| If P&L is... | The bot... |
|---|---|
| **-50% or worse** | Closes the position outright (stop-loss) |
| **+25% to +49%** | "Rolls" it — closes the current contract and immediately opens a fresh one of the same size, in a new expiration window |
| **+50% or better** | Closes the position outright (take-profit) |

**What "rolling" means in plain terms:** instead of just banking the gain
and walking away, the bot locks in the profit on the current contract but
keeps the same directional bet alive by buying a fresh contract with more
time left on the clock. Think of it as "cash in this ticket, buy a new
ticket for the same bet."

If the position gains enough to hit both thresholds at once, closing
takes priority over rolling — a position up 60% closes fully rather than
rolling.

Prices used for these trades: entries buy at the live ask price; exits
and rolls sell at a freshly-checked live bid price right before
submitting. All option orders are limit orders, never market orders — so
on a thin contract, an order can occasionally sit unfilled for a bit even
at a fresh quote.

---

## 6. Why a trade might get blocked

Seeing a rejected/skipped trade is normal, not necessarily a problem.
Common reasons, from most to least common:

| Reason shown in logs/alerts | What it means |
|---|---|
| `session not usable (DEAD)` | The bot couldn't verify the account/positions this cycle (see §9) — no trading at all until it recovers |
| `re-entry locked — flattened this session` | Something in that same position was closed earlier *today* — the bot won't buy back into it again same-day |
| `1 new entry per session already used` | The bot already opened one new position today; only one new entry per calendar day (the SVIX Ladder is the one exception — see §2) |
| `ENABLE_VIX_AUTO_BUY=false` / `ENABLE_VIX_AUTO_SELL=false` | The relevant on/off switch is turned off in settings (currently all three are turned **on**) |
| `VIX_KILL_SWITCH=true — flatten-only` | The manual kill switch is on — new positions are blocked, but the bot can still close existing ones (see §7) |
| `no liquid ... contract found` | No option matching the target expiration/delta window had a real, tradeable market |
| `sized to 0 contracts — exceeds sleeve headroom` | The available budget wasn't enough to buy even one contract |
| `max 3 live orders per cycle reached` | A safety cap — no more than 3 real orders go out in a single check-in, to avoid a bug firing off a runaway stream of trades |
| `order unconfirmed — stopping cycle` | An order was placed but didn't get a clear fill/accept confirmation within 20 seconds — the bot stops rather than risk placing a second order on top of an uncertain one |

---

## 7. The kill switch (manual pause button)

`VIX_KILL_SWITCH` is a setting in the bot's configuration file, `false`
by default. It's **manual only** — nothing in the bot flips it on by
itself.

**When turned on:**
- No new positions of any kind (SVIX rungs, calls, or puts) will open.
- Existing positions can still be closed/sold/taken-profit normally —
  exits are never blocked by the kill switch.

**When turned off (normal state):** everything above operates as
described.

**Important:** flipping it doesn't cancel orders that are already sitting
unfilled — those need to be canceled directly in Alpaca if that's needed.
It also only takes effect the next time the bot checks in, not
instantly — you can force an immediate check by manually triggering the
scheduled job (see §8).

---

## 8. When is the next trade allowed?

Two separate rules control this:

1. **Same-day re-entry lock:** if a position was opened *or* closed today,
   a fresh entry into that same slot won't happen again until the next
   calendar day.
2. **Trading schedule:** the bot only runs on weekdays — there's no
   Saturday/Sunday activity at all, regardless of what the lock says.

So in practice: if something was bought or sold today, the earliest the
bot can act on that same type of trade again is the **next weekday**,
first thing when the morning job runs.

*(The SVIX Ladder is the exception — it isn't subject to the same-day
lock at all, since its own rung history already prevents duplicate buys.)*

---

## 9. System status — the three states

Every time the bot checks in, it first verifies its own connection to the
brokerage account is trustworthy. This produces one of three states:

- **HEALTHY** — everything checks out; new trades and exits are both
  allowed.
- **DEGRADED** — something is slightly off, but existing positions can
  still be closed if the bot has other trustworthy data to confirm what's
  held; no new trades allowed.
- **DEAD** — the bot doesn't trust what it's seeing (e.g., the account
  API isn't responding, or real positions don't match what was expected).
  **No trading of any kind** happens in this state, including closes.
  After going DEAD, there's a built-in 15-minute "cool-down" before it
  will try again — seeing DEAD more than once in a row within that window
  is expected, not a new problem.

This is a deliberate "when in doubt, do nothing" safety design — the bot
would rather sit on its hands than place a trade based on information it
isn't confident in.

---

## 10. System start / stop procedures

The bot runs as two scheduled background jobs (macOS LaunchAgents), not a
program you start and stop yourself day-to-day:

| Job | Runs | What it does |
|---|---|---|
| `com.tvclaude.vix.daily` | 9:00 AM ET, weekdays | One full check-in: verify account → check VIX/market data → decide → trade → log results |
| `com.tvclaude.vix.intraday` | 9:30 AM ET, weekdays | Stays running through the trading day, checking in every 15 minutes, then shuts itself down at end of day |

**To pause the bot from trading (but keep it running/monitoring):** turn
on the kill switch (§7) — this is the recommended way to stop new trades
without disabling monitoring or the ability to close existing positions.

**To fully disable the scheduled jobs** (they won't fire at all, even to
check status):
```
launchctl unload ~/Library/LaunchAgents/com.tvclaude.vix.daily.plist
launchctl unload ~/Library/LaunchAgents/com.tvclaude.vix.intraday.plist
```

**To re-enable them:**
```
launchctl load -w ~/Library/LaunchAgents/com.tvclaude.vix.daily.plist
launchctl load -w ~/Library/LaunchAgents/com.tvclaude.vix.intraday.plist
```

**To force an immediate check-in right now** (without waiting for the
next scheduled time — useful right after changing a setting like the
kill switch):
```
launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.daily
launchctl kickstart -k gui/$(id -u)/com.tvclaude.vix.intraday
```

**To run a single test check-in manually, without touching the
scheduled jobs at all** (safe to run any time):
```
cd "Portfolio Trading System-RH" && venv/bin/python ../loop_intraday_vix.py --once
```
Add `--dry-run` to the end to preview what it *would* do without placing
any real orders or saving any state.

---

## 11. Where to check on it (dashboard)

Start the dashboard with:
```
cd regime_trader && streamlit run dashboard/app.py --server.port 8503
```
Then open the **VIX Trader** tab. It shows:

- **System Health** — is the required API key set, when the last
  successful check-in happened (should never be more than ~15-20 minutes
  old during market hours), and whether the scheduled jobs are currently
  loaded.
- **Account** — real balance/cash/buying power straight from Alpaca.
- **Sleeve P&L** — profit and loss on everything the bot (or you,
  manually) has traded in this account.
- **SVIX Ladder** — is it armed or idle, how high VIX has gotten this
  campaign, what's been bought, how many profit-taking steps are done,
  and any orders that were just submitted and are still waiting on a
  fill confirmation.
- **Bot decision log** — every trade the bot considered that cycle,
  including ones it decided *against* and why. This is the only place
  that shows rejected/skipped trades — they never show up in Alpaca's
  own activity history since they were never actually placed.
- **Manual controls** — buttons to flatten SVIX or close an option
  position by hand, outside of the bot's own automatic logic.

---

## 12. Log files and what the messages mean

Raw logs live in `~/Library/Logs/TVClaude/`:
`vix_daily.log` / `vix_daily.err`, `vix_intraday.log` / `vix_intraday.err`.

Common lines and what they mean:

| Log line | Meaning |
|---|---|
| `state=DEAD reason=...` | The account check failed that cycle (see §9). If it repeats within 15 minutes, that's the expected cooldown, not a new issue. |
| `BOOK_MISMATCH: held:[] but ...` | A specific DEAD cause — real positions came back empty when the bot expected to still see something held. Usually resolves itself once positions settle. |
| `order unconfirmed — stopping cycle` | An order didn't get a clear confirmation in time; the bot stopped rather than risk a duplicate order. |
| `[SUPPRESSED] ...` | The bot decided *not* to send a text alert for this (still logged and visible on the dashboard) — usually a repeat of something already alerted recently, to cut down on noise. |
| `[FATAL] ... crashed` | The script hit an error serious enough to stop it entirely — this always sends a text message immediately. |
| `[intraday] cycle failed (continuing)` | A single check-in cycle hit an error, but the script keeps running and will simply try again at the next interval — no text alert for this one, so check the dashboard's "last check-in" time if things seem quiet for too long. |

**Text message alerts:** the bot sends iMessages for anything noteworthy —
executed trades, rejected trades (with reason), or a DEAD session. The
daily job texts every time it has anything to report; the intraday job
only texts on something notable (not every 15-minute check-in), so a
quiet stretch of the day with no texts is completely normal.

**SKILL.md history:** `Portfolio Trading System-RH/SKILL.md`, under "VIX
Trader — Lessons learned," has one short line added per daily check-in
(posture, VIX level, session state, how many trades happened) — useful
for a quick scan of recent days without digging through logs.

---

## 13. Glossary

- **VIX** — the "fear index"; a measure of expected S&P 500 volatility.
  Higher = more fear/uncertainty in the market.
- **SVIX** — an ETF that moves opposite to VIX; profits when volatility
  falls or stays calm.
- **VXX / UVXY** — ETFs that move *with* VIX; the underlying for the
  bot's call and put options.
- **Rung** — one step in the SVIX Ladder's buying plan (VIX 30, 40, 50,
  etc.) — each one is bought at most once per campaign.
- **Campaign** — one full cycle of the SVIX Ladder, from arming through
  full exit back to idle.
- **Pullback** — the signal (VIX 3% below its campaign peak) that flips
  the Ladder from buying mode to profit-taking mode.
- **DTE** — "days to expiration" — how far out an option contract expires.
- **Delta** — roughly, how sensitive an option's price is to a $1 move in
  the underlying, expressed as a rough proxy for "how likely is this to
  expire in the money."
- **NAV** — net asset value; the total value of the trading account.
- **Sleeve** — the portion of the account dedicated to this bot's
  trading (as opposed to other strategies/positions in the same account).
- **Cost basis** — the average price paid for a currently-held position.
- **Roll** — closing an option position and immediately opening a similar
  new one, typically to lock in a gain while keeping the same bet alive
  with fresh time left on the contract.
- **Session (HEALTHY / DEGRADED / DEAD)** — the bot's own confidence
  level in its connection to the brokerage account that cycle (see §9).
- **Dry run** — a preview mode that runs all the same decision logic but
  never places a real order or saves any state — used for safely testing
  changes.
- **Paper trading** — trading with a simulated account that mirrors real
  market prices and fills, but uses no real money.
- **Kill switch** — the manual pause button that blocks new trades while
  still allowing existing positions to be closed (see §7).
