"""
src/models.py — Core data models for the Transaction Risk Investigation Assistant.
All models are pure dataclasses: no LLM dependencies, fully serialisable to JSON.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Transaction:
    txn_id: str
    date: str          # ISO date string e.g. "2026-01-03"
    description: str
    payee: str
    amount: float
    channel: str       # "card" | "upi" | "neft" | "cash" | "atm"
    hour: int = 12     # 0-23, parsed from timestamp if present

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CustomerProfile:
    customer_id: str
    mean_amount: float
    stddev_amount: float
    max_amount: float          # historical max BEFORE anomaly window
    active_hour_p10: int       # lower bound of normal active hours
    active_hour_p90: int       # upper bound of normal active hours
    payees: dict               # {payee_name: first_seen_date_str}
    channel_mix: dict          # {channel: fraction} e.g. {"upi": 0.7, "card": 0.3}
    dominant_channel: str      # channel with highest fraction
    p95_amount_by_channel: dict  # {channel: p95_amount}


@dataclass
class Finding:
    rule_id: str               # "R1" | "R2" | "R3" | "R4"
    rule_name: str
    severity: str              # "high" | "medium"
    txn_ids: list[str]         # must exist in the input file — validated
    evidence: dict             # rule-specific metrics used to trigger

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReportFinding:
    """LLM-shaped finding — part of the final report returned to the frontend."""
    rule_id: str
    narrative: str
    txn_ids: list[str]
    check_first: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    headline: str
    status: str                # "clean" | "review_recommended" | "investigate"
    findings: list[ReportFinding] = field(default_factory=list)
    source: str = "llm"        # "llm" | "fallback"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
