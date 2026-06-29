import logging
import stripe
from typing import Any, Dict
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.tenant.context import TenantContext
from backend.auth.dependencies import get_tenant_or_api_key
from backend.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

# Initialize Stripe if key is present
if settings.STRIPE_API_KEY:
    stripe.api_key = settings.STRIPE_API_KEY

PLANS = {
    "price_starter": {"name": "Starter", "price": 49, "minutes": 300},
    "price_professional": {"name": "Professional", "price": 199, "minutes": 1500},
    "price_enterprise": {"name": "Enterprise", "price": 499, "minutes": 4500}
}

@router.get("/config")
async def get_billing_config(tenant: TenantContext = Depends(get_tenant_or_api_key)):
    """Return Stripe publishable key and tier packages."""
    return {
        "publishable_key": settings.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY or "pk_test_mock",
        "plans": [
            {"id": pid, **details} for pid, details in PLANS.items()
        ],
        "tenant_id": tenant.tenant_id
    }

@router.post("/checkout")
async def create_checkout_session(
    payload: Dict[str, Any] = Body(...),
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Create Stripe Checkout session. Emulates upgrade if Stripe is not configured."""
    price_id = payload.get("price_id")
    if not price_id or price_id not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid price_id")

    plan = PLANS[price_id]

    # MOCK MODE: Upgrade directly in DB if Stripe credentials are empty
    if not settings.STRIPE_API_KEY:
        logger.warning("Stripe key is missing. Simulating sandbox upgrade for tenant %s", tenant.tenant_id)
        db = get_db()
        await db.tenants.update_one(
            {"tenant_id": tenant.tenant_id},
            {"$set": {
                "tier": plan["name"].lower(),
                "allowed_minutes": plan["minutes"],
                "status": "active",
                "settings.rate_limit_per_minute": 300 if price_id == "price_enterprise" else 150
            }}
        )
        return {
            "checkout_url": f"{settings.DASHBOARD_URL}/dashboard?billing_success=true&tier={plan['name'].lower()}",
            "simulated": True
        }

    try:
        # Create standard Stripe Checkout Session
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                },
            ],
            mode="subscription",
            client_reference_id=tenant.tenant_id,
            success_url=f"{settings.DASHBOARD_URL}/dashboard?billing_success=true&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.DASHBOARD_URL}/dashboard?billing_cancel=true",
            subscription_data={
                # 7 days trial period
                "trial_period_days": 7
            }
        )
        return {"checkout_url": session.url, "simulated": False}
    except Exception as e:
        logger.error("Stripe checkout creation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Checkout creation failed: {e}")

@router.post("/portal")
async def create_customer_portal(
    tenant: TenantContext = Depends(get_tenant_or_api_key),
):
    """Generate customer billing portal link. Simulates return in mock mode."""
    db = get_db()
    tenant_doc = await db.tenants.find_one({"tenant_id": tenant.tenant_id})
    stripe_customer_id = (tenant_doc or {}).get("stripe_customer_id")

    # MOCK MODE: Redirect back to dashboard if Stripe is not configured
    if not settings.STRIPE_API_KEY or not stripe_customer_id:
        return {"portal_url": f"{settings.DASHBOARD_URL}/dashboard"}

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=f"{settings.DASHBOARD_URL}/dashboard",
        )
        return {"portal_url": portal_session.url}
    except Exception as e:
        logger.error("Stripe portal session creation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Customer portal redirect failed: {e}")

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Webhook listener to process stripe billing callbacks and sync tenant packages in MongoDB."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.warning("Stripe webhook received but webhook secret is not configured.")
        return JSONResponse(status_code=400, content={"error": "Webhook secret not configured"})

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid payload"})
    except stripe.error.SignatureVerificationError:
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})

    event_type = event["type"]
    db = get_db()

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        tenant_id = session.get("client_reference_id")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        if tenant_id:
            # Fetch subscription details to know price_id
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                price_id = sub["items"]["data"][0]["price"]["id"]
            except Exception as e:
                logger.error("Failed to retrieve subscription details in webhook: %s", e)
                price_id = "price_starter"  # Fallback

            plan = PLANS.get(price_id, PLANS["price_starter"])

            await db.tenants.update_one(
                {"tenant_id": tenant_id},
                {"$set": {
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": subscription_id,
                    "tier": plan["name"].lower(),
                    "allowed_minutes": plan["minutes"],
                    "status": "active"
                }}
            )
            logger.info("Successfully updated tenant %s to tier %s via webhook checkout", tenant_id, plan["name"])

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        subscription_id = sub.get("id")
        
        # Downgrade tenant to free tier limits
        tenant_doc = await db.tenants.find_one({"stripe_subscription_id": subscription_id})
        if tenant_doc:
            await db.tenants.update_one(
                {"tenant_id": tenant_doc["tenant_id"]},
                {"$set": {
                    "tier": "free",
                    "allowed_minutes": 30, # Base 30 minutes free trial limit
                    "status": "trial_expired"
                }}
            )
            logger.info("Tenant %s subscription expired/cancelled. Reverted to free tier.", tenant_doc["tenant_id"])

    return {"status": "success"}
