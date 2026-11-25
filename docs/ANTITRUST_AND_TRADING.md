# Antitrust risk: how regulatory news moves LITE, MRVL and the spread, and how to trade it

**As of the 2026-11-24 close** (LITE $487.12, MRVL $131.57, gross spread 21.5%). Model: `mergerarb/analytics/antitrust.py`;
run `python scripts/run_antitrust.py` (numbers in [`antitrust_numbers.json`](antitrust_numbers.json)) and
`python scripts/antitrust_event_study.py` (needs local CRSP). Research only, not investment advice.

## Bottom line

1. **Regulatory risk is mostly China, and it is large.**
   - The combined company would be the dominant merchant DSP supplier. A Chinese prospectus puts Broadcom and Marvell at
     about 90% of optical-module DSPs, with Marvell alone at 60-70% of 50-100G/lane PAM4.
   - It would also be a laser and module rival of its own Chinese DSP customers (InnoLight, Hisense, Accelink and others).
   - China's SAMR conditioned the comparable US chip deals (Nvidia-Mellanox, AMD-Xilinx, Broadcom-VMware, Synopsys-Ansys)
     with no-bundling, supply and interoperability remedies after 13-18 months.
   - It let four others lapse (Qualcomm-NXP, Applied-Kokusai, Intel-Tower, DuPont-Rogers) when tensions were high.
   - The base rate is about two thirds approved. The November-2026 climate leans negative: new US AI-chip controls on
     16 Nov, new tariff plans, and a truce extended only to 10 Jan 2027.
2. **Gate by gate, completion is about 48%, in line with the market's ~50%.**
   - US 93%, stockholder votes 81%, EU 94%, China 69%, other 97%.
   - The first report's 63% assumed 80% for all regulators together, which is too generous.
   - **The signal is now NO TRADE**: the expected return is about 2.1% a year below Treasuries at today's spread.
3. **Antitrust worries hit the trade mostly through MRVL, not LITE.**
   - Prices are a probability-weighted mix of "deal" and "no deal" values, and MRVL fell about 21% on the deal.
   - Bad regulatory news therefore lifts MRVL more than it sinks LITE. LITE can even rise if most of MRVL's deal discount
     would reverse on a break.
   - The hedged position loses the same dollars either way: **about $21 per LITE share for every 10-point drop in the
     market's completion odds** (the gap G = spread / (1 - q) = $210 at q = 50%).
4. **To trade it you need a view the market doesn't hold, and patience.**
   - At today's spread, no regulatory view alone clears a 5% hurdle under the base break case. Even a 95% China view only
     takes completion to ~73% against the ~81% needed.
   - The trade works only if you believe LITE would hold about $600 on its own after a break, or if you enter at a wider
     spread (~35%) after a scare.
   - In the news-flow simulation with an edge, waiting for a 35% spread earned ~8.6% a year on capital, against ~4.5%
     for buying today. The rung was hit in about 70% of paths, at an average entry spread of 39%.

## 1. What regulators will look at

| Issue | Evidence | Who cares |
|---|---|---|
| Vertical foreclosure: DSPs | Accelink 2026 prospectus (via your optical-intel DB): Broadcom + Inphi/Marvell ≈ 90% of module DSPs; Marvell 60-70% of 50-100G/lane PAM4. Marvell's documented DSP customers: InnoLight, Hisense, Accelink, Linktel, Source Photonics, Huagong, Lumentum, Coherent (demo). | SAMR (its domestic module makers depend on Marvell DSPs); FTC/DOJ (Coherent, AAOI); EU |
| Vertical foreclosure: lasers | Lumentum is a leading merchant EML/CW laser supplier in a tight market (its gross margin doubled in seven quarters). It also sells transceivers and optical circuit switches. | SAMR, US agencies, hyperscalers |
| Competitor perception | Accelink already lists Marvell (with Coherent and Cisco/Acacia) as a main foreign module competitor. | SAMR |
| Horizontal overlap | Coherent DCI pluggables and optical engines exist on both sides, but they are small next to the vertical issues (not quantified here). | US / EU |
| Remedy appetite | Marvell said it intends to keep its existing component supply model (Disruption Banking, read in full). This is an early signal it will offer behavioural commitments. The merger agreement makes Marvell accept remedies unless they are *material to the combined company* (s6.3(d)). | all |
| Strategic customer | NVIDIA owns LITE preferred stock and has a multi-billion purchase commitment with Lumentum (LITE 8-K, 2026-03-02). Its stance is unknown. | US agencies |

**Timeline anchors from the merger agreement:**
- The first outside date is 2027-11-01. It extends automatically to 2028-02-01, then 2028-05-01, then **2028-08-01** while
  only regulatory conditions remain open.
- Marvell pays **$3.2bn** (~$26 per LITE share after tax) on a regulatory failure.
- One Substack piece gives a "Nov-2028" outside date. That is wrong.

## 2. China's track record with comparable deals (research agent, sourced in `data/derived/antitrust/samr_precedents.csv`)

| Outcome | Deals | Review |
|---|---|---|
| Conditional (behavioural, sometimes divestitures) | Nvidia-Mellanox, AMD-Xilinx, Broadcom-VMware, Synopsys-Ansys | 13-18 months; no bundling, FRAND/continued supply to Chinese customers, interoperability; 6-10 years |
| Unconditional (no remedies found) | Marvell-Cavium, Marvell-Inphi, II-VI-Coherent, Lumentum-NeoPhotonics | 5-15 months; small or no overlap |
| Lapsed (no decision by the outside date) | Qualcomm-NXP, Applied-Kokusai, Intel-Tower, DuPont-Rogers | 12-21 months, in tension periods; never a formal prohibition |

The base rate is 8 of 12 decided cases approved (≈ 67%). Marvell-Lumentum's vertical, China-customer-heavy profile looks
like the *conditional* group, not the quick unconditional one. Expect a long review and behavioural remedies. The tail
risk is a timeout if relations sour. SAMR cleared 420 of 440 filings unconditionally in the first nine months of 2026
(state media; search snippet). Slowing deals down, not prohibiting them, is its lever.

## 3. The mechanism: how a regulatory headline turns into stock moves

Each price is a mix of its completion and break values, weighted by the market's completion probability q:

    M = q*M_C + (1-q)*M_B        L = q*4.5*M_C + (1-q)*S

With G = 4.5*M_B - S (how much more the 4.5 MRVL shares would be worth than LITE after a break):

    spread$ = (1-q)*G      completion P&L = (1-q)*G      break P&L = -q*G      dP&L/dq = G

Today's $104.94 spread pins G down once q is chosen: **$161 at q = 35%, $210 at 50%, $300 at 65%**. The split between
the legs depends on how much of MRVL's ~21% deal discount would reverse on a break.

| A 10-point drop in completion odds (q 50% → 40%) | LITE | MRVL | Gross spread | Hedged P&L per LITE share |
|---|---|---|---|---|
| No MRVL reversal | -4.3% | 0.0% | +5.7 pts | -$21 |
| Half reverses (base) | -1.0% | +2.7% | +4.6 pts | -$21 |
| Full reversal | +2.2% | +5.4% | +3.6 pts | -$21 |

*(From `shock_table`; same P&L by construction. Only the split moves.)*

## 4. What each headline does today

![Headline impacts](figures/antitrust_headlines.png)

Bayesian updates from the base event tree, applied at the market's ~50% odds:

| Headline | Odds now | Odds after | LITE | MRVL | Spread | P&L / LITE sh |
|---|---|---|---|---|---|---|
| HSR second request (75% likely, mostly priced) | 50% | 49% | -0.1% | +0.3% | +0.5 pt | -$3 |
| EU opens phase 2 (40%) | 50% | 45% | -0.5% | +1.3% | +2.2 pts | -$10 |
| SAMR acceptance delayed / pull-and-refile (5%) | 50% | 36% | -1.4% | +3.7% | +6.3 pts | -$29 |
| US agency sues (11%) | 50% | 21% | -2.9% | +7.7% | +13.3 pts | -$60 |
| Any termination (court block, SAMR timeout, failed vote) | 50% | 0% | -5.1% | +13.5% | +23.9 pts | -$105 |
| Both votes pass (81%) | 50% | 62% | +1.2% | -3.2% | -5.2 pts | +$25 |
| SAMR conditional approval | 50% | 72% | +2.3% | -6.1% | -9.9 pts | +$47 |

With full MRVL reversal the P&L column is identical. MRVL moves twice as much, and LITE *rises* on bad news (+6% on a
US suit, +11% on a termination).

## 5. What past antitrust news actually did

![Event study](figures/antitrust_event_study.png)

26 dated events in 17 deals (`data/derived/antitrust/event_study.csv`).
- **Medians:** adverse news +1.2 spread points, terminations +2.8, approvals and clearances -8.2.
- **Surprises are violent:** Capri -49% on the court block (spread +133 pts), NXP -13% on reports China would not approve
  (+16 pts), Activision -11% on the UK block (+12 pts), Tower -10% at termination (+18 pts).
- **Acquirers rallied when deals looked shaky:** Tapestry +15.5%, Qualcomm +6.5%, Kroger +3.7% / +9.4%. Microsoft rose
  8% around the UK block, but that is contaminated by its earnings. This is the same MRVL behaviour the model predicts.
- **Expected steps barely moved prices.** Phase-2 openings and anticipated lawsuits were mostly priced already, and
  approvals tightened spreads sharply (AMD-Xilinx -8.7 pts on China's approval).
- *Caveats:* event dates come from news searches (confidence column in the CSV). The Aetna/Humana and Anthem/Cigna
  lawsuit days show the targets *rising*, which points to other news or an earlier leak. Treat those two as unreliable.
  For stock deals the spread change is the clean measure; raw target returns are swamped by the acquirer's moves.

## 6. Completion probability, gate by gate

| Gate | Pessimist | **Base** | Optimist | Basis |
|---|---|---|---|---|
| US (HSR → second request → consent or suit → court) | 0.89 | **0.93** | 0.93 | second request 75%; suit 11-19%; parties win ~40% in court |
| Both stockholder votes | 0.81 | **0.81** | 0.90 | MRVL votes cast (low bar) but dilution anger; LITE needs a majority of *outstanding*, the offer is below the pre-talks price, CEO admits holders are "not happy" |
| EU | 0.94 | **0.94** | 0.94 | phase 2 40%, clearance after phase 2 85% |
| China SAMR | 0.55 | **0.69** | 0.79 | 2016-25 base rate ≈ 67-69%; vertical + China-customer profile; Nov-2026 escalation |
| No other termination | 0.97 | **0.97** | 0.97 | |
| **P(complete)** | **0.36** | **0.48** | **0.60** | |

## 7. Trading playbook

**Today: no trade.** At a 21.5% spread and 48% completion, the expected return on Reg T capital is ~2.5% a year (≈
-2.1% against Treasuries). Under the stress break case it is negative unless completion odds exceed 69%.

**Where an edge could come from** (`news_mc`: outcomes drawn from *your* tree, prices from the market's; 20,000 paths;
one lot = 2 LITE / 9 MRVL):

| Your view (P complete) | Enter today, hold | Exit on US suit / SAMR slip | Wait for ≥ 35% spread | After US clears | After both votes |
|---|---|---|---|---|---|
| Same as market (48%) | +1.0%/yr | +0.6% | +1.4% | +2.1% | +2.6% |
| More optimistic (60%) | +4.5%/yr | +4.1% | **+8.6%** | +6.2% | +5.9% |
| More pessimistic (36%) | -2.0%/yr | -2.7% | -5.3% | -2.6% | -3.4% |

*(P&L per unit of capital-time, before the opportunity cost of capital. Treasuries pay ~4.6%. The 35% rung was hit in
~70% of paths, at an average entry spread of 39%. It also loses the most in the pessimistic world: a wide spread is not
cheap if the market is right.)*

The edge is all in the view, and in an optimistic world it is best harvested by **waiting for a scare to widen the spread
or for milestones to clear**. A stop on a US lawsuit trims the tail (5% shortfall -8%) but crystallises losses after the
drop, so it lowered the average in every world.

**Rules for this account (USD 10k):**
1. **Entry:**
   - Arm the trigger ladder at **35%** gross spread: one lot, plus a 40% rung only with ≥ $20k equity
     (`execution/trigger.py`).
   - Triggers fire only on **live** two-sided quotes, using the *executable* spread (MRVL bid, LITE ask).
   - Every hit still needs pre-flight checks and your typed approval.
2. **Size:**
   - At most **one 2/9 lot per $10k** (Monte Carlo 95% expected shortfall ≈ $2,000 per lot).
   - Options cannot hedge this: one MRVL option contract covers 100 shares, which is 22 LITE shares (~$10.7k long).
3. **Hold or cut:**
   - Hold through expected steps (second request, EU phase 2): they are mostly priced.
   - Re-assess on a US lawsuit (historically most litigated deals died: Aetna-Humana, Anthem-Cigna, Halliburton-Baker
     Hughes, Tapestry-Capri, Kroger-Albertsons; Microsoft-Activision survived, HPE-Juniper settled). Cut at least half
     unless you rate the court case well above 40%.
   - Cut on a SAMR pull-and-refile *combined with* trade-war escalation.
4. **Hard stops:** borrow fee > 3% or recall notices, excess liquidity < 50% of equity, or any change to the exchange
   ratio.

**Dated catalysts to watch:**
- 10 Jan 2027: US-China truce expiry.
- ~Jan 2027: HSR second request (expected).
- Q1 2027: S-4 filing, which names the required approvals and the merger background.
- ~Mar 2027: SAMR filing accepted.
- ~May 2027: EU phase-1 outcome.
- ~Mid-2027: both votes.
- ~Aug 2027: US decision.
- 1 Nov 2027: first outside date.
- Oct 2027 to Jul 2028: SAMR decision.
- 1 Aug 2028: final outside date.
- Also: any US export control reaching optical components (the clearest negative SAMR signal), and MRVL earnings on
  1 Dec 2026 (re-run scheduled for 2 Dec).

## 8. Being able to trade it: the checklist
1. Install TWS or IB Gateway, log in to the **paper** account, enable the API (127.0.0.1, port 7497/4002), and subscribe
   to US real-time quotes (NASDAQ). Triggers ignore delayed data.
2. `python -m mergerarb.execution.readiness` should print READY. It checks the account type, funds, data type,
   shortability, IBKR's what-if margin for one lot, the session and the executable spread. It places no orders.
3. `python -m mergerarb.execution.service`, then `MERGERARB_ENABLE_EXECUTION=1 streamlit run app/streamlit_app.py`.
   Arm the ladder, wait for a hit, run pre-flight, type the confirmation, and send. The short leg goes first; the target
   is bought only against filled short shares.
4. Paper-trade one full cycle (entry, partial fills, cancel/recover) before considering live mode
   (`MERGERARB_ALLOW_LIVE=1`).

## 9. Provenance and limits
- **Read in full:** the merger agreement, 8-Ks and press release; your optical-intel records (Accelink prospectus quotes
  in Chinese, DSP customer map); Disruption Banking.
- **Search snippets only:** SAMR precedent coverage, US-China climate (China Briefing, Fortune, CNN, SCMP, The National,
  ECNS), commentary pieces.
- **Every probability is judgment.** The tree's branch probabilities, the 35% rung and the reversal fraction are
  parameters; change them in the dashboard's Antitrust tab.
- **Event study:** small sample (26 events) with uncertain dates for some of them; it is used for direction and
  magnitude, not for calibrating probabilities.
