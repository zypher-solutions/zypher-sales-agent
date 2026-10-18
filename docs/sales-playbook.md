# Sales playbook

How the agent sells, and why. Sources: two podcast interviews with the sales trainer Saad (youtu.be/M2OYgUoQwyk, youtu.be/BZv_N4I_THQ; live sales and cold-calling methods, summarized in our own words) and the cold-email research notes we loaded. His methods are for phone and live calls, so each idea below says how it maps to written email, and where we deliberately differ.

## 1. Sales is three parts, and the offer comes first
Offer, distribution, and the sales conversation. A great salesperson cannot rescue a vague offer. For us that means the offer must be fixed before the emails are tuned.

**Offer checklist (his "offer stack"), applied to Zypher**
- Hyper-specific outcome, not "we help you grow". Template: *"I do X through Y so that Z."* One or two named offers, not a service list.
- Name the 5 to 6 obstacles the buyer has, and let the offer answer each one.
- Proof. We have exactly one deployment (see `icp.yaml` honesty rules), so proof is a *free sketch of their own workflow*, not claims.
- Low time and effort for the buyer; a short, defined first step.
- Risk reduction: we will NOT promise guarantees. (He also found guarantees made clients careless.)
- Urgency only if it is real. We do not fake it.
- A named method and a price. **Open item: pricing and the two named offers are still undecided (icp.yaml `pricing_note`).**

## 2. Principles we adopt for email
1. **End every message with a question**, never a statement. Questions steer the reply.
2. **Open with something specific and human**, never a pitch. The first line earns the second.
3. **Pre-handle the likeliest objection inside email 1** ("no call needed", "a one-page sketch").
4. **A ladder of three goals**, in this order: (a) a reply or a meeting, (b) permission to stay in touch, (c) a referral to the right person. Do not stop at the first "no". Email 3 asks "who handles this at your company?".
5. **Agree first, then ask why** when someone objects ("that's fair, what makes it a bad fit?"). Never argue.
6. **Qualify before pitching** (need, decision maker, timeline, budget). Budget is raised last and we never anchor on price first. Disqualify honestly instead of forcing a sale.
7. **Hot vs nurture:** if their decision is near, follow up fast; if months away with a real reason, nurture lightly and never pressure.
8. **Speed:** a positive reply gets a human answer within minutes (Telegram alert). First contact after interest is when interest is highest.
9. **Book with two concrete time options**, not "when are you free", and confirm on a calendar.
10. **Measure the funnel** (sent, replies, positive replies, meetings, proposals) so we fix the leak, not the salesperson.

## 3. Where we deliberately differ
- No fake urgency or scarcity, no "are you lying to me" style challenges, no price games. Our brand is a small business owner emailing another.
- Tonality tactics are for voice calls; in email the equivalent is plain, short, warm wording.
- We do not claim results, clients or numbers we cannot prove (platform guardrail).

## 4. Email sequence (config-driven in icp.yaml `sequence:`, 4 emails over about 4 weeks)
Timing is in WORKING days of the lead's own country after the previous email: step 2 after 3, step 3 after 5 more, step 4 after 9 more. The table below shows the intent; icp.yaml is the source of truth.
| Step | Day | Angle |
|---|---|---|
| 1 | 0 | Specific observation about their workflow, ending with a question; offer a one-page sketch, no call |
| 2 | 3 | A different observation about another workflow; one new idea only |
| 3 | 8 | Ask who owns this at their company (referral ladder), plus our allowed wording for proof |
| 4 | 21 | Breakup: reply 1 interested / 2 not now / 3 stop, then never contact again |

Send Tuesday to Thursday, 9 to 11am recipient local time. Plain text, no links, no images, no open or click tracking. Founder-led: sent from the owner's own address at small volume (10 to 20 a week to start).

## 5. Engine changes this implies (not built yet; each needs approval)
1. `icp.yaml`: add named offers with the "I do X through Y so Z" statement, objections, and sequence angles; first-touch rule becomes question-first with a sketch offer; word range 50 to 80.
2. Guardrails: must end with a question, exactly one ask, no call request in email 1, no urgency words, no links.
3. Phase 2 sequence engine reads the angles from config.
4. Phase 3 reply handling: classify hot vs nurture, ping Telegram immediately, draft a reply that agrees first and proposes two time slots; a human sends.
