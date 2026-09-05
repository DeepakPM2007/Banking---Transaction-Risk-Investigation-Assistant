"""
src/api.py — FastAPI route handlers for the Transaction Risk Investigation Assistant.

Routes:
  GET /health                         → startup check
  GET /api/customers                  → list available demo customers
  GET /api/customers/{id}/transactions → raw transaction data as JSON
  GET /api/customers/{id}/investigate  → run rule engine + LLM, return report
"""

from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.rules import list_customers, load_transactions, build_profile, run_all_rules
from src.llm import generate_report

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/customers")
def get_customers():
    """List all available demo customer IDs."""
    customers = list_customers()
    return {"customers": customers}


@router.get("/customers/{customer_id}/transactions")
def get_transactions(customer_id: str):
    """Return the full transaction history for a customer as JSON."""
    try:
        txns = load_transactions(customer_id)
        return {"customer_id": customer_id, "transactions": [t.to_dict() for t in txns]}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Customer '{customer_id}' not found")
    except Exception as e:
        logger.error(f"Error loading transactions for {customer_id}: {e}")
        raise HTTPException(status_code=500, detail="Error loading transaction data")


@router.get("/customers/{customer_id}/investigate")
def investigate(customer_id: str):
    """
    Run the full investigation pipeline:
    1. Load transactions (pure CSV)
    2. Build customer profile (deterministic)
    3. Run rule engine R1-R4 (deterministic, no LLM)
    4. Send verified findings to Gemini for report synthesis
    5. Validate citations (non-negotiable)
    6. Return structured report JSON

    The status band (clean/review_recommended/investigate) is set by the
    rule engine and cannot be changed by the LLM.
    """
    try:
        txns = load_transactions(customer_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Customer '{customer_id}' not found")

    try:
        profile = build_profile(txns, customer_id)
        status, findings = run_all_rules(txns, profile)

        report = generate_report(
            status=status,
            findings=findings,
            all_txns=txns,
            profile=profile,
        )

        # Build cited transactions map (for frontend to highlight)
        cited_ids = set()
        for f in findings:
            cited_ids.update(f.txn_ids)

        return JSONResponse(content={
            "customer_id": customer_id,
            "report": report.to_dict(),
            "raw_findings": [f.to_dict() for f in findings],
            "profile": {
                "mean_amount": profile.mean_amount,
                "stddev_amount": profile.stddev_amount,
                "max_amount": profile.max_amount,
                "active_hour_p10": profile.active_hour_p10,
                "active_hour_p90": profile.active_hour_p90,
                "dominant_channel": profile.dominant_channel,
            },
        })

    except Exception as e:
        logger.error(f"Investigation error for {customer_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Investigation failed: {str(e)}")
