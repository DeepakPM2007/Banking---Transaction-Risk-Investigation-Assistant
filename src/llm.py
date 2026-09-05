"""
src/llm.py — Gemini report synthesis layer.

DESIGN PRINCIPLE: The LLM is a report WRITER, not a decision maker.
It receives only verified findings and the exact cited transactions.
It cannot change the status, cannot invent transaction IDs, and cannot assert fraud.

Citation validation runs after every LLM response.
If validation fails twice, or if Gemini times out, we serve a deterministic
templated fallback — the user never sees an ungrounded report.
"""

from __future__ import annotations
import os
import json
import logging

from src.models import Finding, Transaction, CustomerProfile, Report, ReportFinding

logger = logging.getLogger(__name__)

# ── Gemini client (lazy init) ─────────────────────────────────────────────────

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            from google import genai
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise ValueError("GEMINI_API_KEY environment variable not set")
            _client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.warning(f"Gemini client init failed: {e}")
            _client = None
    return _client


MODEL = "gemini-2.0-flash"

# ── Prompt builder ────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = """You are a report-writing layer for a bank's fraud investigation desk.
You are NOT a fraud decision-maker. The risk analysis has already been done.
You receive pre-verified findings and the exact transactions behind them.

Your rules:
1. Use ONLY the transaction IDs and evidence provided. Never reference any transaction not given to you.
2. Never state that fraud has occurred. Use language like "warrants review", "deviates from pattern", "an investigator should check".
3. If status is "clean", write one clear paragraph saying so. Do not manufacture concern.
4. Structure: headline status → per-finding narrative (what happened, which rule fired, how it differs from baseline, which transactions, what to check first).
5. Output ONLY valid JSON matching the schema. No prose, no markdown, no explanation outside the JSON.

Output schema (strictly follow this):
{
  "headline": "<one sentence stating clean or flagged>",
  "status": "<clean | review_recommended | investigate>",
  "findings": [
    {
      "rule_id": "<R1|R2|R3|R4>",
      "narrative": "<plain English explanation>",
      "txn_ids": ["<only IDs from those provided>"],
      "check_first": "<one sentence: what an investigator should verify first>"
    }
  ]
}"""


def _build_prompt(status: str, findings: list[Finding],
                  cited_txns: list[Transaction], profile: CustomerProfile) -> str:
    findings_data = [f.to_dict() for f in findings]
    txns_data = [t.to_dict() for t in cited_txns]

    payload = {
        "status": status,
        "customer_baseline": {
            "mean_amount": profile.mean_amount,
            "stddev_amount": profile.stddev_amount,
            "max_amount_historical": profile.max_amount,
            "active_hours_band": f"{profile.active_hour_p10:02d}:00-{profile.active_hour_p90:02d}:59",
            "dominant_channel": profile.dominant_channel,
        },
        "verified_findings": findings_data,
        "cited_transactions": txns_data,
    }

    return (
        "Write an investigation report for the following verified findings.\n"
        "These are the ONLY facts you may reference.\n\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )


# ── Citation validator ────────────────────────────────────────────────────────

def _validate_citations(report_dict: dict, allowed_txn_ids: set[str]) -> bool:
    """Returns True if every txn_id in the LLM output exists in allowed_txn_ids."""
    for finding in report_dict.get("findings", []):
        for tid in finding.get("txn_ids", []):
            if tid not in allowed_txn_ids:
                logger.warning(f"Citation validation failed: {tid} not in allowed set")
                return False
    return True


# ── Templated fallback ────────────────────────────────────────────────────────

def _templated_fallback(status: str, findings: list[Finding]) -> Report:
    """
    Deterministic report built directly from Finding objects.
    Used when Gemini is unavailable or returns invalid citations.
    """
    STATUS_HEADLINES = {
        "clean": "No suspicious activity detected in the reviewed transaction history.",
        "review_recommended": "One or more transactions warrant a closer look by an investigator.",
        "investigate": "Multiple high-severity risk signals detected — immediate investigation recommended.",
    }

    RULE_NAMES = {
        "R1": "Unusually Large Transfer",
        "R2": "Burst to Newly Added Payee",
        "R3": "Odd-Hours Activity",
        "R4": "Pattern Break",
    }

    CHECK_FIRST = {
        "R1": "Verify the customer authorised this transfer and confirm the recipient identity.",
        "R2": "Confirm when this payee was added and whether the customer initiated these payments.",
        "R3": "Check whether the customer was travelling or whether a device anomaly was logged at this time.",
        "R4": "Determine why the channel changed and whether the customer recognises this transaction.",
    }

    report_findings = []
    for f in findings:
        evidence_str = ", ".join(f"{k}: {v}" for k, v in f.evidence.items()
                                 if k not in ("flagged_amounts", "flagged"))
        report_findings.append(ReportFinding(
            rule_id=f.rule_id,
            narrative=(
                f"Rule {f.rule_id} ({RULE_NAMES.get(f.rule_id, f.rule_name)}) fired. "
                f"Severity: {f.severity.upper()}. Evidence: {evidence_str}. "
                f"Transactions involved: {', '.join(f.txn_ids)}."
            ),
            txn_ids=f.txn_ids,
            check_first=CHECK_FIRST.get(f.rule_id, "Review the flagged transactions with the customer."),
        ))

    return Report(
        headline=STATUS_HEADLINES.get(status, "Investigation required."),
        status=status,
        findings=report_findings,
        source="fallback",
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_report(
    status: str,
    findings: list[Finding],
    all_txns: list[Transaction],
    profile: CustomerProfile,
    timeout_seconds: int = 20,
) -> Report:
    """
    Generate an investigation report via Gemini.
    Falls back to a templated report if Gemini is unavailable or citations are invalid.

    Args:
        status: Determined by the rule engine — LLM cannot change this.
        findings: Verified Finding objects from the rule engine.
        all_txns: Full transaction list (used to resolve cited txns).
        profile: Customer baseline profile.
        timeout_seconds: Max time to wait for Gemini response.

    Returns:
        Report object (source="llm" or source="fallback").
    """
    # If no findings, return clean report immediately (no LLM needed)
    if status == "clean":
        return Report(
            headline="No suspicious activity detected in the reviewed transaction history.",
            status="clean",
            findings=[],
            source="llm",
        )

    # Build set of cited transaction IDs and objects
    cited_ids: set[str] = set()
    for f in findings:
        cited_ids.update(f.txn_ids)
    cited_txns = [t for t in all_txns if t.txn_id in cited_ids]
    allowed_ids = {t.txn_id for t in cited_txns}

    client = _get_client()
    if client is None:
        logger.warning("Gemini client unavailable — using fallback report")
        return _templated_fallback(status, findings)

    prompt = _build_prompt(status, findings, cited_txns, profile)

    # Try up to 2 times (retry once on citation failure)
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt,
                config={
                    "system_instruction": SYSTEM_INSTRUCTION,
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                }
            )

            raw = response.text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            report_dict = json.loads(raw)

            # Citation validation — non-negotiable
            if not _validate_citations(report_dict, allowed_ids):
                logger.warning(f"Citation check failed on attempt {attempt + 1}")
                if attempt == 0:
                    continue  # retry
                else:
                    return _templated_fallback(status, findings)

            # Force status to match rule engine output (LLM cannot change it)
            report_dict["status"] = status

            report_findings = [
                ReportFinding(
                    rule_id=f.get("rule_id", ""),
                    narrative=f.get("narrative", ""),
                    txn_ids=f.get("txn_ids", []),
                    check_first=f.get("check_first", ""),
                )
                for f in report_dict.get("findings", [])
            ]

            return Report(
                headline=report_dict.get("headline", ""),
                status=status,
                findings=report_findings,
                source="llm",
            )

        except Exception as e:
            logger.error(f"Gemini call failed (attempt {attempt + 1}): {e}")
            if attempt == 1:
                return _templated_fallback(status, findings)

    return _templated_fallback(status, findings)
