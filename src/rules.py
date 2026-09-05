"""
src/rules.py — Deterministic rule engine for transaction risk investigation.

DESIGN PRINCIPLE: Zero LLM calls. Every function is a pure Python computation
over transaction data. Output is structured Finding objects, not prose.
The banding logic (clean / review_recommended / investigate) is also computed
here and is FINAL — the LLM report layer cannot change it.
"""

from __future__ import annotations
import csv
import os
import math
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

from src.models import Transaction, CustomerProfile, Finding


# ── CSV loader ────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "customers")


def load_transactions(customer_id: str) -> list[Transaction]:
    """Load and parse a customer CSV into Transaction objects."""
    path = os.path.join(DATA_DIR, f"{customer_id}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No data file for customer '{customer_id}' at {path}")

    txns = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            hour = 12  # default
            if row.get("timestamp"):
                try:
                    hour = datetime.fromisoformat(row["timestamp"]).hour
                except ValueError:
                    pass
            txns.append(Transaction(
                txn_id=row["txn_id"],
                date=row["date"],
                description=row.get("description", ""),
                payee=row.get("payee", ""),
                amount=float(row["amount"]),
                channel=row.get("channel", "upi"),
                hour=hour,
            ))
    return txns


def list_customers() -> list[str]:
    """Return all available customer IDs (CSV stems in data/customers/)."""
    if not os.path.isdir(DATA_DIR):
        return []
    return [
        f[:-4] for f in os.listdir(DATA_DIR)
        if f.endswith(".csv")
    ]


# ── Profile builder ───────────────────────────────────────────────────────────

def build_profile(txns: list[Transaction], customer_id: str = "") -> CustomerProfile:
    """Derive a CustomerProfile from the transaction history."""
    amounts = [t.amount for t in txns]
    hours = [t.hour for t in txns]

    mean_amt = statistics.mean(amounts) if amounts else 0.0
    stddev_amt = statistics.stdev(amounts) if len(amounts) > 1 else 0.0
    max_amt = max(amounts) if amounts else 0.0

    sorted_hours = sorted(hours)
    n = len(sorted_hours)
    p10_hour = sorted_hours[max(0, int(n * 0.10))] if n else 8
    p90_hour = sorted_hours[min(n - 1, int(n * 0.90))] if n else 22

    # Payee first-seen map
    payees: dict[str, str] = {}
    for t in sorted(txns, key=lambda x: x.date):
        if t.payee not in payees:
            payees[t.payee] = t.date

    # Channel mix
    channel_counts: dict[str, int] = defaultdict(int)
    for t in txns:
        channel_counts[t.channel] += 1
    total = len(txns) or 1
    channel_mix = {ch: cnt / total for ch, cnt in channel_counts.items()}
    dominant_channel = max(channel_mix, key=channel_mix.get) if channel_mix else "upi"

    # p95 amount by channel
    p95_by_channel: dict[str, float] = {}
    for ch in channel_counts:
        ch_amounts = sorted([t.amount for t in txns if t.channel == ch])
        idx = min(len(ch_amounts) - 1, int(len(ch_amounts) * 0.95))
        p95_by_channel[ch] = ch_amounts[idx] if ch_amounts else 0.0

    return CustomerProfile(
        customer_id=customer_id,
        mean_amount=round(mean_amt, 2),
        stddev_amount=round(stddev_amt, 2),
        max_amount=round(max_amt, 2),
        active_hour_p10=p10_hour,
        active_hour_p90=p90_hour,
        payees=payees,
        channel_mix=channel_mix,
        dominant_channel=dominant_channel,
        p95_amount_by_channel=p95_by_channel,
    )


# ── Rule R1: Unusually Large Transfer ────────────────────────────────────────

def rule_large_transfer(txns: list[Transaction], profile: CustomerProfile) -> list[Finding]:
    """
    R1: Flag transactions where amount > mean + 3*stddev OR > 2x customer historical max.
    Severity: High
    """
    findings = []
    threshold_stat = profile.mean_amount + 3 * profile.stddev_amount
    threshold_max  = profile.max_amount * 2.0

    triggered = [
        t for t in txns
        if t.amount > threshold_stat or t.amount > threshold_max
    ]

    if triggered:
        findings.append(Finding(
            rule_id="R1",
            rule_name="Unusually Large Transfer",
            severity="high",
            txn_ids=[t.txn_id for t in triggered],
            evidence={
                "customer_mean": round(profile.mean_amount, 2),
                "customer_stddev": round(profile.stddev_amount, 2),
                "stat_threshold": round(threshold_stat, 2),
                "max_threshold": round(threshold_max, 2),
                "flagged_amounts": {t.txn_id: t.amount for t in triggered},
            }
        ))
    return findings


# ── Rule R2: Burst to Newly Added Payee ──────────────────────────────────────

def rule_new_payee_burst(txns: list[Transaction], profile: CustomerProfile) -> list[Finding]:
    """
    R2: Flag payees first seen < 7 days ago that received >=2 payments
    totalling > 3x customer average within that window.
    Severity: High
    """
    findings = []
    NEW_PAYEE_DAYS = 7
    MIN_PAYMENTS = 2
    TOTAL_MULTIPLIER = 3.0

    # Find newest date in the dataset (= "today" for the window check)
    all_dates = sorted(set(t.date for t in txns))
    if not all_dates:
        return findings
    latest_date = datetime.strptime(all_dates[-1], "%Y-%m-%d")
    window_start = (latest_date - timedelta(days=NEW_PAYEE_DAYS)).strftime("%Y-%m-%d")

    # Payees first seen within the window
    new_payees = {
        payee: first_seen
        for payee, first_seen in profile.payees.items()
        if first_seen >= window_start
    }

    for payee, first_seen in new_payees.items():
        burst_txns = [
            t for t in txns
            if t.payee == payee and t.date >= first_seen
        ]
        total_sent = sum(t.amount for t in burst_txns)
        threshold = profile.mean_amount * TOTAL_MULTIPLIER

        if len(burst_txns) >= MIN_PAYMENTS and total_sent > threshold:
            findings.append(Finding(
                rule_id="R2",
                rule_name="Burst to Newly Added Payee",
                severity="high",
                txn_ids=[t.txn_id for t in burst_txns],
                evidence={
                    "payee": payee,
                    "first_seen": first_seen,
                    "payment_count": len(burst_txns),
                    "total_sent": round(total_sent, 2),
                    "threshold_3x_avg": round(threshold, 2),
                    "customer_mean": round(profile.mean_amount, 2),
                }
            ))
    return findings


# ── Rule R3: Odd-Hours Activity ───────────────────────────────────────────────

def rule_odd_hours(txns: list[Transaction], profile: CustomerProfile) -> list[Finding]:
    """
    R3: Flag transactions outside the customer's own p10-p90 active-hours band.
    Severity: Medium
    """
    findings = []
    lo = profile.active_hour_p10
    hi = profile.active_hour_p90

    triggered = [
        t for t in txns
        if t.hour < lo or t.hour > hi
    ]

    if triggered:
        findings.append(Finding(
            rule_id="R3",
            rule_name="Odd-Hours Activity",
            severity="medium",
            txn_ids=[t.txn_id for t in triggered],
            evidence={
                "customer_active_band": f"{lo:02d}:00 – {hi:02d}:59",
                "active_hour_p10": lo,
                "active_hour_p90": hi,
                "flagged_hours": {t.txn_id: t.hour for t in triggered},
            }
        ))
    return findings


# ── Rule R4: Pattern Break ────────────────────────────────────────────────────

def rule_pattern_break(txns: list[Transaction], profile: CustomerProfile) -> list[Finding]:
    """
    R4: Flag transactions that:
      (a) use a channel that represents <20% of the customer's normal mix, AND
      (b) the amount exceeds the p95 for that channel.
    Severity: Medium
    """
    findings = []
    RARE_CHANNEL_THRESHOLD = 0.20  # channel used less than 20% of the time

    triggered = []
    for t in txns:
        ch_fraction = profile.channel_mix.get(t.channel, 0.0)
        p95 = profile.p95_amount_by_channel.get(t.channel, 0.0)

        is_rare_channel = ch_fraction < RARE_CHANNEL_THRESHOLD
        is_high_amount  = t.amount > p95

        if is_rare_channel and is_high_amount:
            triggered.append(t)

    if triggered:
        findings.append(Finding(
            rule_id="R4",
            rule_name="Pattern Break",
            severity="medium",
            txn_ids=[t.txn_id for t in triggered],
            evidence={
                "dominant_channel": profile.dominant_channel,
                "dominant_channel_fraction": round(profile.channel_mix.get(profile.dominant_channel, 0), 3),
                "rare_channel_threshold": RARE_CHANNEL_THRESHOLD,
                "flagged": [
                    {
                        "txn_id": t.txn_id,
                        "channel": t.channel,
                        "channel_fraction": round(profile.channel_mix.get(t.channel, 0), 3),
                        "amount": t.amount,
                        "p95_for_channel": round(profile.p95_amount_by_channel.get(t.channel, 0), 2),
                    }
                    for t in triggered
                ],
            }
        ))
    return findings


# ── Master runner ─────────────────────────────────────────────────────────────

RULES = [rule_large_transfer, rule_new_payee_burst, rule_odd_hours, rule_pattern_break]


def run_all_rules(txns: list[Transaction], profile: CustomerProfile) -> tuple[str, list[Finding]]:
    """
    Run all four rules. Compute status band from findings.
    Status is determined HERE — the LLM cannot change it.

    Returns:
        (status, findings)
        status: "clean" | "review_recommended" | "investigate"
    """
    all_findings: list[Finding] = []
    for rule_fn in RULES:
        all_findings.extend(rule_fn(txns, profile))

    if not all_findings:
        status = "clean"
    elif any(f.severity == "high" for f in all_findings):
        status = "investigate"
    else:
        status = "review_recommended"

    return status, all_findings


def investigate_customer(customer_id: str) -> tuple[str, list[Finding], list[Transaction], CustomerProfile]:
    """
    Full pipeline: load → profile → run rules.
    Returns (status, findings, all_txns, profile).
    """
    txns = load_transactions(customer_id)
    profile = build_profile(txns, customer_id)
    status, findings = run_all_rules(txns, profile)
    return status, findings, txns, profile
