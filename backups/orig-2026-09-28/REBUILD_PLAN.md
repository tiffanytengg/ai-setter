# AI Setter — 4-Day Rebuild Plan

**Client:** Committed Coaches · **Owner:** Tiffany Teng
**Window:** Sept 27 → demo Sept 30 (buffer Oct 1)
**Status:** Architecture reset — the n8n workflow is burned down to a thin orchestration layer; reasoning moves to an agent service + MCP servers.
**Rev. 3** — adds the two-phase channel strategy (texts first, then emails).

## 0. The one thing that matters

The agent must send a real booking or reschedule message to a real phone, live, during the demo.

Everything else exists to make that possible or to explain it. Any day that doesn't move the send path forward is a day spent wrong.

## 1. Day 0 — Data recon (pre-step, done Sept 27)

### Scope, settled from the SOW
- The committed scope is **triage of inbound text messages** (scheduling/rescheduling primary). The webhook normalizes text *and* email events from GHL and Close, but nothing asks us to send or triage email.
- The SOW promised "historical inbound text messages" as test data; the 168 samples delivered were **emails**. They become supplementary training data — the real golden set is mined from SMS threads (below). Report SMS accuracy as the headline metric, honestly.
- **Two-phase channel strategy (texts first, then emails).** Phase 1 = the SOW scope: inbound texts, booking/reschedule live, shadow mode for the rest. Phase 2 = the same flows over email through the *identical* pipeline — channel is a parameter on the canonical message, not a new system. The 168 emails, once a scope puzzle, become the **phase-2 golden set**, ready-made. Nothing in the architecture changes between phases; only the final send tool differs (`send_sms` → `send_email`).

### Close — where the data actually lives
- 'Setter Email View' (84k leads) and 'Asked to Reschedule but have not yet' (3k) are **Smart Views — saved lead searches**, not message streams. Setter activity lives in the **Conversations** stream and lead timelines.
- Access confirmed: the Close user sees all Conversations threads (personal inbox is empty and the user is not an admin — neither matters). Right org confirmed — Adam: "all appointment texts are sent in the main sales sub-account."
- **How to find setter SMS:** search SMS content for a distinctive phrase from the booking template (template text = the search key). Then read the sender + From number off the hits. No special inbox access needed.
- **Link resolution — solved.** The booking link is per-client configured; the email macro is `{{ user.scheduling_links.main_setter }}` — it resolves from the **sending user's profile**. `render_template` resolves per sender; a static account→link map is the grounded fallback if the API doesn't expose it.
- **Setters personalize the template** (text varies per send) — yes, they edit before sending. Consequence: the agent sending the exact template is *more* consistent than current human practice. A demo selling point.
- The template goes out at thread start after an **unknown trigger** (lead created? purchase?) — a production question, parked for the POC.

### GHL — intake already specified
- The sales sub-account runs a "Customer Replied" workflow (SMS, optionally email) that POSTs every inbound message to `https://n8n.committedcoaches.com/webhook/tt-inbound-crm`. Observation-only; **re-entry must be on** or each contact fires only once.
- **Payload warning:** the workflow-webhook format (contact object + message/trigger fields) is *not* the sample-messages shape. The normalise step must be built from **real captured executions**, not the samples.
- Ground rules (production sub-account): read-only; any write needs Adam's OK; experiments only to designated test contacts. **The allowlist in our architecture is literally this rule, enforced in code.**
- The non-prod test environment from the SOW was not built; Adam OK'd reading production. Test contact still TBD.

### The consolidated Adam ask
1. Which test contact/number for end-to-end testing?
2. OK to send a test SMS to my own phone from the main sales org?
3. Does my API key have send permission, or which key should I use?
4. Where is the sending user's `scheduling_links.main_setter` stored — readable via API, or static map for the demo?

## 2. What changes, and what doesn't

### Diagnosis — why the n8n build couldn't get here
1. The action layer was never built — WF-02 had no credential and no verified send path.
2. The classification loop was untestable — edit-run-query through a UI meant bugs took hours to find.
3. No separation of concerns — credentials, business logic, data access, and reasoning lived in one workflow graph.

### What stays
- **n8n as the thin orchestration layer.** Fixed SOW requirement; it becomes the event bus (~6 nodes), not the brain. The `tt-inbound-crm` webhook node is retained.
- **The Postgres schema.** Six tables, working queries, real data. It earned its place — the new setter-memory server is an access layer over it, not a replacement.
- **The 168-message email dataset** — as supplementary training data, not the headline eval.
- **Everything documented** — decision log, considerations log, the three findings from the check-in.

### What moves
| Currently in n8n | Moves to |
|---|---|
| Classification prompts and parsing | Agent service (Claude Agent SDK) |
| Template rendering (+ per-sender link resolution) | close-gateway MCP server |
| Business rules and allowlist | Enforced inside gateway servers |
| Context lookups | close-gateway / ghl-gateway |
| Message logging | setter-memory MCP server (same Postgres) |

n8n keeps: webhook intake (`tt-inbound-crm`), normalisation, calling the agent service, dispatching the result, error handling.

**SOW defense:** "Use of the provided n8n environment is a fixed requirement" — satisfied, it remains the orchestration layer. "Design decisions above the orchestration layer are Tiffany's to make" — this is that decision, documented explicitly.

## 3. Target architecture

```
Customer SMS
    │
Close / GoHighLevel ── webhook ──▶ n8n WF-01 (thin)
    │                    (tt-inbound-crm)  receive, normalise, call agent,
    │                                     dispatch, log
    │                                    HTTP
    │                                     ▼
    │                        Agent service (Claude Agent SDK)
    │                         pre-pass → relevance filter →
    │                         classify → select skill → execute
    │                               │ MCP tools
    │               ┌───────────────┼───────────────┐
    │               ▼               ▼               ▼
    │        close-gateway    ghl-gateway    setter-memory
    │        (Close API)     (GHL API)      (Postgres)
```

### Why three gateways, not one
The MCP boundary is the credential and trust domain, not "is it an external API."
- **Blast radius.** A compromised GHL gateway cannot reach Close. Credentials never share a process.
- **Audit trail.** The agent calls `close.send_sms`, not `send_message(source="close")`. The tool name records which system was touched.
- **Independent deployment.** A GHL API version bump doesn't redeploy the Close path.
- **Selective capability grants.** Read tools from one server, write tools from neither.

### Where enforcement lives
MCP tools are things an agent chooses to call — a safety check exposed as a tool can be skipped. So the allowlist and business rules are **enforced inside the gateway servers, never offered as tools**. `close.send_sms` checks the recipient against the allowlist as part of the send and refuses otherwise. The agent cannot route around it. This is the project's own ground rule ("experiments only to designated test contacts"), enforced in code.

> The guardrail isn't a tool the agent is asked to call — it's inside the only thing that can send.

### Server inventory
| Server | Tools | Credential | Notes |
|---|---|---|---|
| close-gateway | get_message, get_lead, render_template, send_sms, send_email (Phase 2) | Close API key | send_sms/send_email enforce allowlist internally; render_template resolves `{{ user.scheduling_links.main_setter }}` per sender |
| ghl-gateway | get_contact, get_conversation | GHL PIT (sales sub-account) | Read-only this week; no send tool |
| setter-memory | log_message, log_decision, get_state | Postgres | No external network access at all |

## 4. Request lifecycle (replaces the 23-node graph)

1. **Webhook → n8n (thin).** `tt-inbound-crm` receives; normalise GHL workflow payloads and Close payloads into one canonical message. **Build normalisation from real captured executions**, not the sample files — the shapes differ. Drop outbound / own-team messages (else the system replies to itself).
2. **Deterministic pre-pass (code, no model).** Opt-outs (compliance — never reach a model), duplicates (webhook retries → idempotency key; note GHL re-entry is on by design).
3. **Cheap relevance filter (Haiku).** One question on the raw text: *is this about scheduling at all?* Removes ~90% of coaching traffic before expensive work.
4. **Agent (Claude Agent SDK).** Loads contact state via setter-memory; classifies with structured output (intent, confidence, reasoning); selects a skill.
5. **Skill executes via MCP tools.** Active skills may send; shadow skills only log the intended action and escalate.
6. **Every step logged** via setter-memory → the decision record that makes accuracy measurable.

## 5. Skills

Behaviour lives in versioned skill files, not prompt strings in a UI. Channel is a parameter: the same skill classifies once and sends via `send_sms` (Phase 1) or `send_email` (Phase 2) — trigger, template logic, and guardrails don't change.

**Active — can send:**
| Skill | Trigger | Action |
|---|---|---|
| send-booking-link | Lead wants to book a first call | Renders booking template (per-sender link), sends via close.send_sms |
| send-reschedule-link | Lead wants to move/rebook | Renders reschedule template, sends via close.send_sms |

**Shadow — logs only, never acts:**
| Skill | Trigger | Action |
|---|---|---|
| handle-refund | Billing dispute / refund request | Records intended action, escalates to Slack |
| handle-cancellation | Wants to cancel the programme | Same |
| handle-tech-issue | Login / access problem | Same |

**Deterministic — never reaches the agent:** opt-outs, duplicates.

## 6. Day-by-day

### Day 1 — Prove the send path (nothing else starts until this works)
- Get Adam's answers first: test contact, OK to text my own phone, API key send permission (ground rules: writes need his OK).
- Verify Close auth; identify the sending number/user from a real setter SMS thread.
- Create a test lead pointed at my own mobile; attempt a real SMS send.
- Send the actual booking template; **click the link** and confirm it resolves the right per-client booking page.
- Define the MCP tool contracts (even as stubs); initialise the repo; stop hand-editing anything.
- **Exit:** a message sent from code to a real phone, with a verified-correct link. If Close cannot send: immediate escalation — same hour, not next day.

### Day 2 — Agent service: classify and act
- **Eval harness first.** Hand-label 40–60 of the trickiest messages; build the harness before the classifier so every prompt change is measurable in seconds.
- **Golden set, honestly built.** Mine real SMS threads via template-phrase search (the headline eval); 168 emails as supplementary training. Report accuracy on the hand-labeled SMS slice.
- **Classifier.** Message in → structured decision out (intent, confidence, reasoning). Intent taxonomy from the real data: scheduling, rescheduling, coaching check-in, refund, cancellation, technical, opt-out, other.
- **Skills that send.** send-booking-link + send-reschedule-link end to end against the Day-1 test lead. Shadow skills log + escalate.
- **Exit:** the agent running against a real message produces a sent text; accuracy measured.

### Day 3 — Gateways + thin n8n
- **close-gateway:** get_message, get_lead, render_template (per-sender `scheduling_links.main_setter` resolution, static map fallback), send_sms with the allowlist check inside the send.
- **ghl-gateway:** get_contact, get_conversation — read-only. No send tool this week.
- **setter-memory:** log_message, log_decision, get_state over the existing Postgres. No new schema.
- **Deploy separately** (Railway — Postgres already lives there). Three services, three credential sets.
- **Rebuild WF-01 thin** around the retained `tt-inbound-crm` webhook: normalise (from real captured payloads) → call agent service → dispatch → log. Keep WF-03 error handling as-is.
- **Exit:** the agent operates entirely through MCP tools. No direct API calls in agent code.

### Day 4 — Integrate, rehearse, document
- **The full loop, twenty times.** Text the Close number from my phone → webhook → agent classifies → skill renders and sends → phone buzzes. Run it until boring; find failures today, not at the demo.
- **Batch replay.** The SMS golden set through the new pipeline → funnel + accuracy numbers for the deck.
- **Documentation** (per the SOW: architecture, assumptions/decisions, limitations, success metrics, production cost estimate) — consolidate existing material around the new architecture, don't write from scratch.
- **Rehearse the demo twice**, out loud, including the live send.

### Phase 2 — email (stretch, if Phase 1 lands early)
- Add `send_email` to close-gateway (same allowlist, same shape as `send_sms`).
- Run the 168-email set through the identical pipeline → email accuracy numbers.
- Demo beat: the same booking flow answering an email thread. No new architecture — the point is that there *is* no new architecture.

## 7. Demo structure
1. **Live send first.** Text the number from my phone; show the reply arriving. Lead with the win.
2. **Architecture** — the three servers, and why the guardrail lives inside the gateway rather than beside it.
3. **Measured accuracy** on real SMS messages (emails disclosed as supplementary).
4. **Shadow handling** — refunds/cancellations classified, deliberately not acted on.
5. **Production path** — what changes, what it costs, what is needed.
6. **Phase 2 preview (if time).** The same pipeline handling an email thread — one system, two channels, multiple flows at once.

## 8. Cut lines
- If Days 3–4 compress: demo a working send + measured classifier, present the MCP architecture with close-gateway actually deployed. A working send with a described architecture beats a perfect architecture with nothing sending.
- Do not rebuild the logging. Do not add unfamiliar tools — learning cost comes out of the same four days.

## 9. Risks
- **Day 1 send-path failure is the only unrecoverable risk** — now including write permission (Adam's OK). Mitigation: the Adam ask goes out first, same hour escalation.
- **Link resolves wrong/empty via API.** Fallback: static account→link map inside `render_template`, flagged temporary (now a grounded fallback — we know the macro and its semantics).
- **Payload shape surprises.** Mitigated: normalisation is built from real captured executions per the GHL doc's explicit warning.
- **Scope creep into GHL multi-account.** 182 accounts and the agency-level app are production-estimate material, not this week's work.

## 10. Open questions
1. ~~Which user/account must the agent send as?~~ → Resolved: the sender of the appointment texts in the main sales org; link resolves from their profile.
2. Test contact + write permission for the demo send (asked).
3. Does the API key have send permission on the Close org?
4. Is `scheduling_links.main_setter` readable via API, or static map for the demo?
5. What triggers the thread-start template? (Production question, parked.)
6. Daily inbound volume, for the cost model.
7. Named reviewer for ongoing accuracy measurement.
8. Which Close users are setters vs coaches? (Needed to measure against the right traffic.)

## 11. Production path
- Capability-scoped services: the reasoning layer cannot send; only the gateway can, and only to authorised recipients.
- Independent credentials per external system; blast radius bounded by design.
- Data plane isolated: the memory server has no external network access.
- Behaviour as versioned files: skills are reviewable and diffable.
- Measurement built in: every decision logged with reasoning, so accuracy is computed, not asserted.
- Access: agency-level GHL application covering all locations (vs. one token per account).
- Oversight: human approval step before send; named reviewer ~15 min/day.

---
*Framing for the walkthrough: this is a judgment layer in front of infrastructure that already works. The templates, the links, and the sending all exist. What takes a person's time is reading every inbound message to find the minority that needs action — that is what this automates, with a human retaining the final decision wherever it matters.*
