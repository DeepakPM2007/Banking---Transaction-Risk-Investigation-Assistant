# PS06 — Transaction Risk Investigation Assistant
## Implementation Spec (NexusTiq24 Hackathon)

> Hand this whole file to a coding assistant (e.g. Claude Code) as the build brief.
> Track: `TRACK_ID=PS06`

---

## 1. Problem, restated precisely

Input: **one customer's transaction history** (date, description, payee, amount, channel) spanning several months — mostly routine, sometimes not.

The system must:
1. Run the history against a **small, fixed set of risk rules**.
2. Produce an **investigation report** whose *first line* states whether anything needs attention at all.
3. When something is flagged: show the specific transactions involved, how they connect, which rule fired, how this deviates from the customer's own normal pattern, and what an investigator should check first.
4. When nothing is flagged: say so plainly. No manufactured suspicion.
5. **Every transaction cited must be traceable to the input file.** The system must **never assert fraud** — only flag, explain, and hand off judgement.

Hard constraints from the master brief (apply to whichever track you build):
- Backend: Python. `pip install -r requirements.txt` then `python app.py` serves **everything** (API + built frontend) on `http://localhost:8000`.
- Only external API: **Gemini** (`GEMINI_API_KEY` env var, never committed). Use it for LLM calls; use `gemini-embedding-001` only if you actually need embeddings (this track doesn't strictly need them — see §7 optional enhancement).
- No other network calls. Local FAISS/Chroma/sqlite/numpy only, if used at all.
- App must start and be answering within **90 seconds**; any single request must resolve within **60 seconds**.
- Repo root must contain `app.py`, `requirements.txt`, `README.md`. First line of README: `TRACK_ID=PS06` (nothing else on that line).
- Commit data/build artifacts, not venvs/keys/model weights.
- Real, incremental commit history across the build — not one final dump.

---

## 2. Architecture — the core design decision

**Strict separation of deterministic logic and LLM reasoning** (this is an explicit judging criterion — lead with it in the README too):

```
CSV/JSON transaction history
        │
        ▼
┌─────────────────────────────┐
│  Rule Engine (pure Python)  │   <- deterministic, unit-testable, zero LLM
│  - no ML, no LLM calls      │
│  - outputs structured JSON  │
│  findings: [{rule_id, txn_ids, severity, evidence}]
└──────────────┬──────────────┘
               │ (only structured findings + relevant txns go to the LLM —
               │  never the raw full history, to keep prompts small & grounded)
               ▼
┌─────────────────────────────┐
│  Gemini (report writer)     │   <- turns verified findings into prose
│  - cannot invent findings   │
│  - must cite txn IDs given  │
│  - forbidden from saying    │
│    "fraud confirmed"        │
└──────────────┬──────────────┘
               ▼
      Structured Investigation Report (JSON) → rendered in frontend
```

The LLM is **never** the thing that decides "is this suspicious" — the rule engine decides that. The LLM's only job is synthesis/explanation. This makes the system auditable and dramatically reduces hallucination risk, and it's exactly what the eval rubric rewards ("clear separation between LLM reasoning and deterministic logic").

---

## 3. Data model

### Input: `data/customers/<customer_id>.csv`
```
txn_id,date,description,payee,amount,channel
T0001,2026-01-03,POS purchase,Big Bazaar,842.50,card
T0002,2026-01-04,UPI transfer,Amit Kumar,1500.00,upi
...
```
- `txn_id` is the unit everything cites back to.
- Generate 3-6 months of data per customer (150-400 rows is plenty).

### Customer profile (derived, not stored separately — computed by the rule engine at load time)
- avg transaction amount, stddev
- typical active hours (e.g. 8am-10pm)
- established payee list + first-seen date per payee
- typical channel mix

---

## 4. Rule engine — the four rules named in the brief

Each rule is a small, independently testable Python function. Suggested thresholds (tune via your synthetic data, document them in README):

| Rule ID | Name | Logic | Severity |
|---|---|---|---|
| R1 | Unusually large transfer | `amount > mean + 3*stddev` (or `> N× the customer's own historical max`) for that customer | High |
| R2 | Burst to newly added payee | A payee whose `first_seen` is within last **X days** receives **≥N** payments totalling **≥Y** within that window | High |
| R3 | Odd-hours activity | Transaction timestamp falls outside the customer's own established active-hours band (derived from their own history, not a global constant) | Medium |
| R4 | Pattern break | Sudden change in typical channel, amount band, or geography/description pattern vs. the customer's own rolling baseline | Medium |

```python
# src/rules.py — sketch
def rule_large_transfer(customer_txns, profile) -> list[Finding]: ...
def rule_new_payee_burst(customer_txns, profile) -> list[Finding]: ...
def rule_odd_hours(customer_txns, profile) -> list[Finding]: ...
def rule_pattern_break(customer_txns, profile) -> list[Finding]: ...

RULES = [rule_large_transfer, rule_new_payee_burst, rule_odd_hours, rule_pattern_break]

@dataclass
class Finding:
    rule_id: str
    rule_name: str
    severity: str            # "high" | "medium"
    txn_ids: list[str]       # must exist in the input file — validate this
    evidence: dict           # e.g. {"amount": 55000, "customer_avg": 3200, "customer_max_prior": 9000}
```

**Escalation banding** (maps to "first line states whether anything needs attention"):
- 0 findings → `status: clean`
- ≥1 medium, 0 high → `status: review_recommended`
- ≥1 high → `status: investigate`
- Conflicting/borderline (e.g. only 1 weak medium signal) → still `review_recommended`, never silently upgraded to `investigate` by the LLM. The banding is rule-engine output, full stop — the LLM does not get to change it.

---

## 5. LLM layer (Gemini) — prompt contract

Send the LLM **only**:
- The `status` and list of `Finding` objects (already computed)
- The specific cited transactions (not the whole history)
- The customer's baseline profile numbers used in the evidence

**System instruction (paraphrase, don't copy verbatim into code comments — write your own wording):**
> You are a report-writing layer for a fraud *desk*, not a fraud decision-maker. You are given already-verified findings and the exact transactions behind them. Write a clear investigation report from them. Rules:
> - Use only the transaction IDs and evidence provided. Never reference a transaction not in the input.
> - Never state that fraud has occurred. Use language like "warrants review," "deviates from pattern," "an investigator should check."
> - If `status` is `clean`, say so plainly in one paragraph — do not manufacture concern.
> - Structure output as: headline status → per-finding narrative (what happened, which rule, how it differs from baseline, transactions involved, what to check first) → nothing else.
> - Output valid JSON matching the given schema. No prose outside the JSON.

**Output schema (LLM must return this shape):**
```json
{
  "headline": "string — one sentence, states clean or not",
  "status": "clean | review_recommended | investigate",
  "findings": [
    {
      "rule_id": "R1",
      "narrative": "string",
      "txn_ids": ["T0042", "T0043"],
      "check_first": "string"
    }
  ]
}
```

**Post-generation validation (Python, deterministic, non-negotiable):**
- Every `txn_id` the LLM outputs must exist in the transactions actually sent to it. Reject/retry once if not; on second failure, fall back to a templated report built directly from `Finding` objects (no LLM) — **never show the user an ungrounded report.**
- If the Gemini call fails or times out, serve the templated fallback report immediately rather than hanging — this satisfies "graceful behaviour when a model call fails."

---

## 6. API design (FastAPI recommended, but Flask is fine)

```
GET  /                         → serves built frontend (static files)
GET  /api/customers             → list available demo customers
GET  /api/customers/{id}/investigate
                                 → runs rule engine + LLM synthesis, returns report JSON
GET  /api/customers/{id}/transactions
                                 → raw transactions (for the UI to show alongside citations)
GET  /health                    → for the 90s-startup check
```

Keep the investigate endpoint **synchronous and fast**: rule engine is instant (pure Python over a few hundred rows); the only latency is one Gemini call. Set a client-side timeout comfortably under the 60s request limit (e.g. 20s) with the templated fallback as the safety net.

---

## 7. Frontend (keep it simple — judges care about the reasoning, not the UI)

Minimal single-page app (plain HTML/JS is enough, or a small React build committed to `frontend/dist/`):
- Dropdown: pick a demo customer.
- "Run Investigation" button → calls `/api/customers/{id}/investigate`.
- Renders: headline banner (clean=green / review=yellow / investigate=red), then each finding as a card showing narrative + a table of the exact cited transactions (pulled from `/transactions`, filtered to `txn_ids` — this visually proves the citation is real).
- A raw "full transaction history" table below, for the judge to sanity-check the citations themselves.

## 7b. Optional enhancement if time allows (not required)
Use `gemini-embedding-001` to cluster payee-description text for fuzzy "is this really a new payee" detection (catching near-duplicate payee names). Nice-to-have, not core — don't let it eat your 24 hours.

---

## 8. Data generation plan

Write `scripts/generate_data.py` that produces 2-3 demo customers:
1. **`clean_customer`** — pure routine data, no rule fires. This is your "normal case" for the demo video.
2. **`layered_anomaly_customer`** — deliberately seed 2-3 rules firing together with a coherent story (e.g. a new payee added, then a burst of transfers to them, one of which is also unusually large and at 3am). This is your "difficult case" and should look like a real narrative when the LLM writes it up.
3. *(Optional)* a **borderline customer** — one weak medium signal only, to demonstrate the "doesn't cry wolf" discipline in the README/video.

Use an LLM (Gemini, or your own script) to generate realistic payee names/descriptions, then programmatically inject the anomaly patterns so you know exactly what should be detected — this makes testing and the demo reliable.

---

## 9. Repo shape (matches the brief exactly)

```
your-project/
  app.py                   # starts backend + serves frontend on :8000
  requirements.txt
  README.md                # first line: TRACK_ID=PS06
  src/
    rules.py               # deterministic rule engine
    llm.py                 # Gemini call + schema validation + fallback
    models.py              # Finding, Report dataclasses
    api.py                 # route handlers
  data/
    customers/*.csv        # generated demo data (committed)
  scripts/
    generate_data.py       # regenerate demo data on demand
  frontend/dist/            # built static frontend (committed)
  tests/
    test_rules.py           # unit tests per rule — cheap and impressive to judges
```

---

## 10. README checklist

1. `TRACK_ID=PS06` as the literal first line.
2. What it does (2-3 sentences).
3. How to run (`pip install -r requirements.txt && python app.py`, then open `http://localhost:8000`).
4. `GEMINI_API_KEY` — how it's read, note it's never committed.
5. What data/documents you generated and how (`scripts/generate_data.py`).
6. Explicit callout of the rule-engine/LLM separation (§2 diagram or a one-paragraph version) — make the judges' job easy.
7. Link to the 2-3 min demo video.

---

## 11. Suggested 24-hour build order (for real, incremental commits)

1. Repo skeleton + `app.py` returning "hello" on :8000. **Commit.**
2. Data generator producing `clean_customer` + `layered_anomaly_customer`. **Commit.**
3. Rule engine (R1-R4) + unit tests against known-seeded anomalies. **Commit.**
4. `/api/customers/*` endpoints wired to rule engine, no LLM yet (return templated report). **Commit.**
5. Gemini integration + schema validation + citation-check + fallback path. **Commit.**
6. Minimal frontend hitting the API. **Commit.**
7. Borderline customer + polish narratives/thresholds using real output. **Commit.**
8. README, fresh-clone test, record demo video. **Commit.**

---

## 12. What NOT to build (judgement, per the rubric)

- No live bank feed / real account integration — synthetic CSV is explicitly expected and fine.
- No attempt to have the LLM "decide" fraud — it only narrates verified findings. Keep this boundary visible in code and in the demo narration; it's the single biggest score driver for "well-grounded GenAI implementation."
- No heavyweight vector DB — not needed for this track's actual requirement; don't add complexity the problem doesn't ask for.
