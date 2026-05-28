"""
Billing Routes
==============
  GET  /api/v1/billing/plans      — Return all available pricing plans
  GET  /api/v1/billing/usage      — Return current key's usage vs. tier limits
  POST /api/v1/billing/checkout   — Create a Stripe Checkout Session (if STRIPE_SECRET_KEY set)
  POST /api/v1/billing/portal     — Create Stripe Customer Portal URL
  POST /api/v1/billing/webhook    — Stripe webhook receiver (tier upgrades)
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ..dependencies.auth import require_api_key, require_admin


router = APIRouter(prefix="/billing", tags=["billing"])

# ── Pricing plans ─────────────────────────────────────────────────────────

PLANS: list[dict] = [
    {
        "id": "free",
        "name": "Free",
        "price_monthly": 0,
        "price_yearly": 0,
        "currency": "usd",
        "stripe_price_id_monthly": None,
        "stripe_price_id_yearly": None,
        "features": [
            "50 API calls / day",
            "1 saved profile",
            "Template library access",
            "PDF fill & extract",
        ],
        "limits": {
            "calls_per_day": 10000,
            "profiles": 1,
            "batch_records_per_job": 100,
            "signing_sessions": 3,
        },
        "cta": "Get Started",
        "highlighted": False,
    },
    {
        "id": "pro",
        "name": "Pro",
        "price_monthly": 29,
        "price_yearly": 290,
        "currency": "usd",
        "stripe_price_id_monthly": "price_pro_monthly",
        "stripe_price_id_yearly": "price_pro_yearly",
        "features": [
            "100,000 API calls / day",
            "Unlimited saved profiles",
            "Bulk CSV/Excel fill",
            "E-signature + multi-party signing",
            "Webhook integrations",
            "Email notifications",
            "Priority support",
        ],
        "limits": {
            "calls_per_day": 100000,
            "profiles": -1,
            "batch_records_per_job": 1000,
            "signing_sessions": -1,
        },
        "cta": "Start Free Trial",
        "highlighted": True,
    },
    {
        "id": "business",
        "name": "Business",
        "price_monthly": 99,
        "price_yearly": 990,
        "currency": "usd",
        "stripe_price_id_monthly": "price_business_monthly",
        "stripe_price_id_yearly": "price_business_yearly",
        "features": [
            "1,000,000 API calls / day",
            "Everything in Pro",
            "Zapier integration",
            "Advanced audit logs",
            "SLA uptime guarantee",
            "Dedicated onboarding",
        ],
        "limits": {
            "calls_per_day": 1000000,
            "profiles": -1,
            "batch_records_per_job": 10000,
            "signing_sessions": -1,
        },
        "cta": "Contact Sales",
        "highlighted": False,
    },
]


@router.get("/plans", summary="Get all pricing plans")
async def get_plans():
    """Returns all pricing plans with features and limits. No auth required."""
    stripe_configured = bool(getattr(settings, "STRIPE_SECRET_KEY", ""))
    return {
        "plans": PLANS,
        "stripe_enabled": stripe_configured,
        "currency": "usd",
    }


# ── Usage for current key ─────────────────────────────────────────────────

@router.get("/usage", summary="Get usage stats for the current API key", dependencies=[Depends(require_api_key)])
async def get_usage(request: Request):
    """Returns usage counters and tier limits for the authenticated API key."""
    key_record: dict = request.state.api_key
    tier = key_record.get("tier", "free")
    plan = next((p for p in PLANS if p["id"] == tier), PLANS[0])

    usage_count = key_record.get("usage_count", 0)
    created_at = key_record.get("created_at", "")
    last_used = key_record.get("last_used", "")

    return {
        "key_id": key_record.get("id"),
        "tier": tier,
        "plan_name": plan["name"],
        "usage": {
            "total_api_calls": usage_count,
            "created_at": created_at,
            "last_used": last_used,
        },
        "limits": plan["limits"],
        "upgrade_available": tier == "free",
        "stripe_enabled": bool(getattr(settings, "STRIPE_SECRET_KEY", "")),
    }


# ── Stripe Checkout ────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_id: str
    billing_period: str = "monthly"
    success_url: str = f"{settings.APP_BASE_URL}/ui/billing.html?checkout=success"
    cancel_url: str = f"{settings.APP_BASE_URL}/ui/billing.html?checkout=cancel"


@router.post("/checkout", summary="Create a Stripe Checkout session", dependencies=[Depends(require_api_key)])
async def create_checkout(body: CheckoutRequest, request: Request):
    """
    Creates a Stripe Checkout session for upgrading the current API key's tier.
    Requires STRIPE_SECRET_KEY to be set in .env.
    Returns a redirect URL for the Stripe-hosted checkout page.
    """
    stripe_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not stripe_key:
        raise HTTPException(
            503,
            "Stripe is not configured. Set STRIPE_SECRET_KEY in your .env file. "
            "Get your key at https://dashboard.stripe.com/apikeys",
        )

    plan = next((p for p in PLANS if p["id"] == body.plan_id), None)
    if not plan:
        raise HTTPException(400, f"Unknown plan '{body.plan_id}'.")
    if plan["price_monthly"] == 0:
        raise HTTPException(400, "The Free plan does not require checkout.")

    price_id = (
        plan["stripe_price_id_yearly"]
        if body.billing_period == "yearly"
        else plan["stripe_price_id_monthly"]
    )
    if not price_id:
        raise HTTPException(400, f"No Stripe price configured for plan '{body.plan_id}'.")

    try:
        import stripe  # type: ignore
        stripe.api_key = stripe_key

        key_record: dict = request.state.api_key
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            metadata={
                "fillmypdf_key_id": key_record.get("id", ""),
                "target_tier": body.plan_id,
            },
            client_reference_id=key_record.get("id", ""),
        )
        return {"checkout_url": session.url, "session_id": session.id}
    except ImportError:
        raise HTTPException(
            503,
            "The 'stripe' Python package is not installed. Run: pip install stripe",
        )
    except Exception as exc:
        raise HTTPException(500, f"Stripe error: {exc}")


# ── Stripe Customer Portal ─────────────────────────────────────────────────

class PortalRequest(BaseModel):
    return_url: str = f"{settings.APP_BASE_URL}/ui/billing.html"


@router.post("/portal", summary="Create Stripe Customer Portal session", dependencies=[Depends(require_api_key)])
async def create_portal(body: PortalRequest, request: Request):
    """Opens the Stripe Customer Portal for managing subscription, invoices, and payment methods."""
    stripe_key = getattr(settings, "STRIPE_SECRET_KEY", "")
    if not stripe_key:
        raise HTTPException(503, "Stripe is not configured.")

    key_record: dict = request.state.api_key
    customer_id = key_record.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(
            400,
            "No Stripe customer linked to this API key yet. Complete a checkout first.",
        )

    try:
        import stripe  # type: ignore
        stripe.api_key = stripe_key
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=body.return_url
        )
        return {"portal_url": session.url}
    except ImportError:
        raise HTTPException(503, "stripe package not installed.")
    except Exception as exc:
        raise HTTPException(500, f"Stripe error: {exc}")


# ── Stripe Webhook ─────────────────────────────────────────────────────────

@router.post("/webhook", summary="Stripe webhook receiver (tier upgrades)", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None, alias="stripe-signature")):
    """
    Receives Stripe webhook events and upgrades the API key tier when a
    checkout.session.completed event is received.
    Set STRIPE_WEBHOOK_SECRET in .env and point the Stripe dashboard to:
    POST /api/v1/billing/webhook
    """
    webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    body = await request.body()

    if webhook_secret and stripe_signature:
        # Verify Stripe signature
        try:
            import stripe  # type: ignore
            stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", "")
            event = stripe.Webhook.construct_event(body, stripe_signature, webhook_secret)
        except Exception as exc:
            raise HTTPException(400, f"Webhook signature invalid: {exc}")
    else:
        import json
        try:
            event = json.loads(body)
        except Exception:
            raise HTTPException(400, "Invalid JSON payload.")

    event_type = event.get("type", "")

    if event_type == "checkout.session.completed":
        data = event.get("data", {}).get("object", {})
        key_id = data.get("metadata", {}).get("fillmypdf_key_id") or data.get("client_reference_id")
        target_tier = data.get("metadata", {}).get("target_tier", "pro")
        customer_id = data.get("customer")

        if key_id:
            try:
                from ...services.api_key_service import APIKeyService
                svc = APIKeyService()
                key_record = svc.get_by_id(key_id)
                if key_record:
                    key_record["tier"] = target_tier
                    if customer_id:
                        key_record["stripe_customer_id"] = customer_id
                    svc.update(key_id, key_record)
            except Exception as exc:
                print(f"[billing webhook] Failed to upgrade key {key_id}: {exc}")

    if event_type == "customer.subscription.deleted":
        data = event.get("data", {}).get("object", {})
        customer_id = data.get("customer")
        if customer_id:
            try:
                from ...services.api_key_service import APIKeyService
                svc = APIKeyService()
                key_record = svc.get_by_customer_id(customer_id)
                if key_record:
                    key_record["tier"] = "free"
                    svc.update(key_record["id"], key_record)
            except Exception as exc:
                print(f"[billing webhook] Failed to downgrade customer {customer_id}: {exc}")

    return {"received": True, "type": event_type}
