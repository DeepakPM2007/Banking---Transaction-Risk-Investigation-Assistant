"""
tests/test_rules.py — Unit tests for the deterministic rule engine.
Each rule is tested in isolation against known-seeded data.
Run: python -m pytest tests/ -v
"""

import pytest
from datetime import datetime, timedelta

from src.models import Transaction, CustomerProfile
from src.rules import (
    build_profile,
    rule_large_transfer,
    rule_new_payee_burst,
    rule_odd_hours,
    rule_pattern_break,
    run_all_rules,
    load_transactions,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_txn(txn_id="T0001", date="2026-03-01", payee="Test Payee",
             amount=1000.0, channel="upi", hour=10):
    return Transaction(
        txn_id=txn_id, date=date, description="Test",
        payee=payee, amount=amount, channel=channel, hour=hour
    )


def baseline_txns(n=50, amount=1000.0, channel="upi", hour=10):
    """Generate n routine transactions with controllable defaults."""
    base = datetime(2026, 1, 1)
    return [
        make_txn(
            txn_id=f"T{i:04d}",
            date=(base + timedelta(days=i)).strftime("%Y-%m-%d"),
            payee=f"Payee{i % 5}",
            amount=amount + (i % 5) * 100,  # increased variance
            channel=channel,
            hour=hour,
        )
        for i in range(n)
    ]


# ── R1: Large Transfer ────────────────────────────────────────────────────────

class TestR1LargeTransfer:
    def test_fires_on_3sigma_amount(self):
        txns = baseline_txns(50, amount=1000.0)
        profile = build_profile(txns)
        # Inject a huge transaction
        big = make_txn("TBIG", "2026-03-01", amount=50000.0)
        txns_with_big = txns + [big]
        profile2 = build_profile(txns_with_big)
        findings = rule_large_transfer(txns_with_big, profile2)
        assert len(findings) == 1
        assert findings[0].rule_id == "R1"
        assert "TBIG" in findings[0].txn_ids

    def test_fires_when_exceeds_2x_max(self):
        txns = baseline_txns(10, amount=5000.0)
        profile = build_profile(txns)
        big = make_txn("TBIG2", amount=profile.max_amount * 2.5)
        findings = rule_large_transfer([big], profile)
        assert any("TBIG2" in f.txn_ids for f in findings)

    def test_does_not_fire_on_normal_amount(self):
        txns = baseline_txns(50, amount=1000.0)
        profile = build_profile(txns)
        normal = make_txn("TNORM", amount=1200.0)
        findings = rule_large_transfer([normal], profile)
        assert findings == []

    def test_severity_is_high(self):
        txns = baseline_txns(50, amount=1000.0)
        profile = build_profile(txns)
        big = make_txn("TBIG3", amount=50000.0)
        findings = rule_large_transfer(txns + [big], build_profile(txns + [big]))
        assert all(f.severity == "high" for f in findings)


# ── R2: New Payee Burst ───────────────────────────────────────────────────────

class TestR2NewPayeeBurst:
    def _make_burst(self, n_payments=3, amount=8000.0):
        base_date = datetime(2026, 5, 20)
        txns = baseline_txns(100, amount=1000.0)  # establish history
        # Add burst to a brand-new payee in the last 7 days
        latest_date = datetime(2026, 5, 28)
        burst = [
            make_txn(
                txn_id=f"TNEW{i}",
                date=(latest_date - timedelta(days=i)).strftime("%Y-%m-%d"),
                payee="NewSuspectPayee",
                amount=amount,
                channel="upi",
            )
            for i in range(n_payments)
        ]
        return txns + burst

    def test_fires_on_new_payee_burst(self):
        txns = self._make_burst(n_payments=3, amount=8000.0)
        profile = build_profile(txns)
        findings = rule_new_payee_burst(txns, profile)
        assert len(findings) >= 1
        assert findings[0].rule_id == "R2"
        assert findings[0].severity == "high"

    def test_does_not_fire_for_single_payment(self):
        txns = baseline_txns(100, amount=1000.0)
        latest = datetime(2026, 5, 28)
        single = make_txn("TSINGLE", date=latest.strftime("%Y-%m-%d"),
                          payee="OneTimePayee", amount=5000.0)
        txns_all = txns + [single]
        profile = build_profile(txns_all)
        findings = rule_new_payee_burst(txns_all, profile)
        # Should not fire for just 1 payment
        assert not any("OneTimePayee" in str(f.evidence) for f in findings)


# ── R3: Odd Hours ─────────────────────────────────────────────────────────────

class TestR3OddHours:
    def test_fires_on_3am_transaction(self):
        txns = baseline_txns(80, hour=12)  # all transactions at noon
        profile = build_profile(txns)
        night_txn = make_txn("T3AM", hour=3)
        findings = rule_odd_hours([night_txn], profile)
        assert len(findings) == 1
        assert findings[0].rule_id == "R3"
        assert "T3AM" in findings[0].txn_ids
        assert findings[0].severity == "medium"

    def test_does_not_fire_within_normal_band(self):
        txns = [make_txn(f"T{i}", hour=10 + (i % 8)) for i in range(80)]  # 10 to 17
        profile = build_profile(txns)
        normal = make_txn("TNOON", hour=14)
        findings = rule_odd_hours([normal], profile)
        assert findings == []


# ── R4: Pattern Break ─────────────────────────────────────────────────────────

class TestR4PatternBreak:
    def test_fires_on_rare_channel_high_amount(self):
        # Customer uses UPI 95% of the time, rarely NEFT
        txns = baseline_txns(90, channel="upi", amount=1000.0)
        txns += baseline_txns(5, channel="neft", amount=500.0)
        for i, t in enumerate(txns):
            t.txn_id = f"T{i:04d}"
        profile = build_profile(txns)

        # A very large NEFT when NEFT is only ~5% of transactions
        big_neft = make_txn("TNEFT_BIG", channel="neft", amount=50000.0)
        findings = rule_pattern_break([big_neft], profile)
        assert len(findings) == 1
        assert findings[0].rule_id == "R4"
        assert findings[0].severity == "medium"

    def test_does_not_fire_for_dominant_channel(self):
        txns = baseline_txns(90, channel="upi", amount=1000.0)
        profile = build_profile(txns)
        # High amount on dominant channel — should NOT fire R4
        upi_txn = make_txn("TUPI_HIGH", channel="upi", amount=3000.0)
        findings = rule_pattern_break([upi_txn], profile)
        assert findings == []


# ── Integration: full customer datasets ──────────────────────────────────────

class TestIntegration:
    def test_clean_customer_returns_clean(self):
        try:
            txns = load_transactions("clean_customer")
            profile = build_profile(txns)
            status, findings = run_all_rules(txns, profile)
            assert status == "clean", f"Expected clean, got {status}. Findings: {findings}"
        except FileNotFoundError:
            pytest.skip("clean_customer.csv not generated — run scripts/generate_data.py")

    def test_anomaly_customer_returns_investigate(self):
        try:
            txns = load_transactions("layered_anomaly_customer")
            profile = build_profile(txns)
            status, findings = run_all_rules(txns, profile)
            assert status == "investigate", f"Expected investigate, got {status}"
            assert len(findings) >= 2, f"Expected multiple findings, got {findings}"
        except FileNotFoundError:
            pytest.skip("layered_anomaly_customer.csv not generated — run scripts/generate_data.py")

    def test_status_banding_logic(self):
        """Status banding is purely from findings — not LLM-modifiable."""
        from src.models import Finding
        high = Finding("R1", "Large Transfer", "high", ["T001"], {})
        medium = Finding("R3", "Odd Hours", "medium", ["T002"], {})

        txns = baseline_txns(10)
        profile = build_profile(txns)

        # Simulate: 0 findings -> clean
        status, _ = run_all_rules(txns, profile)
        assert status == "clean"
