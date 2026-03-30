from fastapi import APIRouter, Depends, HTTPException, Request
from api.auth import get_current_user
from db import get_user_credits, add_credits, get_pool
from config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, FRONTEND_URL

router = APIRouter()

CREDIT_PACKAGES = [
    {"id": "starter",   "credits": 100,  "price_cents": 499,  "label": "Starter",  "popular": False},
    {"id": "pro",       "credits": 500,  "price_cents": 1499, "label": "Pro",       "popular": True},
    {"id": "power",     "credits": 2000, "price_cents": 2999, "label": "Power",     "popular": False},
]


@router.get("/")
async def get_balance(user=Depends(get_current_user)):
    credits = await get_user_credits(user["user_id"])
    return {"credits": round(credits, 1)}


@router.get("/packages")
async def get_packages():
    return CREDIT_PACKAGES


@router.post("/checkout/{package_id}")
async def create_checkout(package_id: str, user=Depends(get_current_user)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payments not configured yet")

    pkg = next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"ApplyAgent — {pkg['credits']} Credits",
                        "description": f"Use credits to auto-apply to jobs ({pkg['credits']} applications)",
                    },
                    "unit_amount": pkg["price_cents"],
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{FRONTEND_URL}/dashboard?payment=success",
            cancel_url=f"{FRONTEND_URL}/pricing",
            metadata={
                "user_id": str(user["user_id"]),
                "credits": str(pkg["credits"]),
                "package_id": package_id,
            },
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe calls this after a successful payment to add credits."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            import json
            event = json.loads(payload)

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            meta = session.get("metadata", {})
            user_id = int(meta.get("user_id", 0))
            credits = float(meta.get("credits", 0))
            if user_id and credits:
                await add_credits(user_id, credits)
                print(f"  ✓ Added {credits} credits to user {user_id}")

        return {"received": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
