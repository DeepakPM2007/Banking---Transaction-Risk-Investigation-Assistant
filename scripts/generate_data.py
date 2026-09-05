"""
scripts/generate_data.py — Generates synthetic demo customer transaction data.
Run: python scripts/generate_data.py
Produces data/customers/clean_customer.csv and data/customers/layered_anomaly_customer.csv
"""

import csv
import os
import random
from datetime import datetime, timedelta

random.seed(42)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "customers")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Realistic Indian payees / descriptions ────────────────────────────────────

ROUTINE_PAYEES_UPI = [
    "Rahul Sharma", "Priya Mehta", "Suresh Iyer", "Anita Reddy",
    "Vikram Nair", "Kavitha Krishnan", "Ramesh Patel", "Sunita Joshi",
    "Arun Verma", "Deepa Pillai",
]
ROUTINE_PAYEES_CARD = [
    "Big Bazaar", "Reliance Fresh", "DMart", "Spencer's Retail",
    "More Supermarket", "Nature's Basket", "Decathlon", "Lifestyle",
    "Shoppers Stop", "Croma",
]
ROUTINE_PAYEES_NEFT = [
    "HDFC Housing Loan", "LIC Premium", "Tata AIG Insurance",
    "SBI Mutual Fund", "Axis Securities",
]
ROUTINE_PAYEES_ATM = ["ATM Withdrawal"]
ROUTINE_PAYEES_CASH = ["Cash Deposit"]

CHANNELS = ["upi", "card", "neft", "atm", "cash"]
CHANNEL_WEIGHTS_CLEAN = [0.50, 0.30, 0.10, 0.07, 0.03]

DESCRIPTIONS_UPI   = ["UPI transfer", "UPI payment", "Sent via PhonePe", "Sent via GPay", "UPI P2P"]
DESCRIPTIONS_CARD  = ["POS purchase", "Contactless payment", "Debit card swipe", "Card payment"]
DESCRIPTIONS_NEFT  = ["NEFT transfer", "Online bank transfer", "NEFT outward", "EMI debit"]
DESCRIPTIONS_ATM   = ["ATM cash withdrawal", "ATM withdrawal"]
DESCRIPTIONS_CASH  = ["Cash deposit", "Branch cash deposit"]

CHANNEL_PAYEES = {
    "upi":  ROUTINE_PAYEES_UPI,
    "card": ROUTINE_PAYEES_CARD,
    "neft": ROUTINE_PAYEES_NEFT,
    "atm":  ROUTINE_PAYEES_ATM,
    "cash": ROUTINE_PAYEES_CASH,
}
CHANNEL_DESCS = {
    "upi":  DESCRIPTIONS_UPI,
    "card": DESCRIPTIONS_CARD,
    "neft": DESCRIPTIONS_NEFT,
    "atm":  DESCRIPTIONS_ATM,
    "cash": DESCRIPTIONS_CASH,
}


def random_hour_normal():
    """Returns hour in normal business range 8am-10pm (weighted toward daytime)."""
    return random.choices(range(8, 23), weights=[3,5,7,9,9,8,7,6,6,5,5,4,3,2,1], k=1)[0]


def make_txn(txn_id, date, channel, payee=None, amount=None, hour=None, desc=None):
    ch = channel
    p = payee or random.choice(CHANNEL_PAYEES[ch])
    d = desc or random.choice(CHANNEL_DESCS[ch])
    h = hour if hour is not None else random_hour_normal()
    timestamp = f"{date}T{h:02d}:{random.randint(0,59):02d}:00"
    return {
        "txn_id": txn_id,
        "date": date,
        "timestamp": timestamp,
        "description": d,
        "payee": p,
        "amount": round(amount if amount is not None else random.uniform(200, 5000), 2),
        "channel": ch,
    }


def date_range(start_date, days):
    return [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


# ── Clean Customer ────────────────────────────────────────────────────────────

def generate_clean_customer(filename="clean_customer.csv"):
    start = datetime(2026, 1, 1)
    dates = date_range(start, 180)
    rows = []
    tid = 1

    for date in dates:
        # 0-2 txns per day
        n = random.choices([0, 1, 2], weights=[0.3, 0.5, 0.2])[0]
        for _ in range(n):
            ch = random.choices(CHANNELS, weights=CHANNEL_WEIGHTS_CLEAN)[0]
            amount = random.uniform(300, 6000)
            rows.append(make_txn(f"T{tid:04d}", date, ch, amount=amount))
            tid += 1

    write_csv(os.path.join(OUTPUT_DIR, filename), rows)
    print(f"Generated {len(rows)} transactions -> {filename}")


# ── Layered Anomaly Customer ──────────────────────────────────────────────────

def generate_layered_anomaly_customer(filename="layered_anomaly_customer.csv"):
    start = datetime(2026, 1, 1)
    dates = date_range(start, 180)
    rows = []
    tid = 1

    # 1. Build 5 months of clean baseline (Jan–May, ~150 days)
    baseline_dates = dates[:150]
    baseline_payees = ROUTINE_PAYEES_UPI[:6] + ROUTINE_PAYEES_CARD[:4]

    for date in baseline_dates:
        n = random.choices([0, 1, 2], weights=[0.3, 0.5, 0.2])[0]
        for _ in range(n):
            ch = random.choices(CHANNELS, weights=CHANNEL_WEIGHTS_CLEAN)[0]
            payee = random.choice(CHANNEL_PAYEES[ch])
            amount = random.uniform(300, 5000)
            rows.append(make_txn(f"T{tid:04d}", date, ch, payee=payee, amount=amount))
            tid += 1

    # --- Anomaly window: last 30 days (June) ---
    anomaly_dates = dates[150:]

    # Add routine txns during anomaly window too
    for date in anomaly_dates:
        n = random.choices([0, 1], weights=[0.4, 0.6])[0]
        for _ in range(n):
            ch = random.choices(CHANNELS, weights=CHANNEL_WEIGHTS_CLEAN)[0]
            payee = random.choice(CHANNEL_PAYEES[ch])
            amount = random.uniform(300, 4000)
            rows.append(make_txn(f"T{tid:04d}", date, ch, payee=payee, amount=amount))
            tid += 1

    # R2 + R3 + R1: New payee "Ravi Malhotra" added recently → burst of transfers,
    # one of which is also very large and at 3am
    new_payee = "Ravi Malhotra"
    burst_dates = [anomaly_dates[2], anomaly_dates[3], anomaly_dates[5]]

    # R2+R3 — transfer at 3am to new payee
    rows.append(make_txn(f"T{tid:04d}", burst_dates[0], "upi",
                         payee=new_payee, amount=8500.00, hour=3,
                         desc="UPI transfer - Ravi Malhotra"))
    tid += 1

    # R2 — another transfer to same new payee (large)
    rows.append(make_txn(f"T{tid:04d}", burst_dates[1], "upi",
                         payee=new_payee, amount=9200.00, hour=14,
                         desc="UPI P2P - Ravi Malhotra"))
    tid += 1

    # R1 + R2 — unusually large transfer to same payee
    rows.append(make_txn(f"T{tid:04d}", burst_dates[2], "upi",
                         payee=new_payee, amount=55000.00, hour=2,
                         desc="UPI transfer - urgent payment Ravi Malhotra"))
    tid += 1

    # R4 — sudden channel switch: customer normally uses UPI/card, now large NEFT
    rows.append(make_txn(f"T{tid:04d}", anomaly_dates[8], "neft",
                         payee="Offshore Trade Solutions", amount=28000.00, hour=11,
                         desc="NEFT outward - Offshore Trade Solutions"))
    tid += 1

    # Sort by date+txn_id for clean CSV
    rows.sort(key=lambda r: (r["date"], r["txn_id"]))

    write_csv(os.path.join(OUTPUT_DIR, filename), rows)
    print(f"Generated {len(rows)} transactions -> {filename}")


def write_csv(path, rows):
    fieldnames = ["txn_id", "date", "timestamp", "description", "payee", "amount", "channel"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    generate_clean_customer()
    generate_layered_anomaly_customer()
    print("Done. Data written to data/customers/")
