"""FS.8.1 -- Stripe checkout / billing portal endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _auth
from backend.config import settings
from backend.stripe_billing import (
    StripeBillingConfig,
    StripeBillingConfigError,
    StripeBillingError,
    create_billing_portal_session,
    create_checkout_session,
    session_response,
)


router = APIRouter(
    prefix="/billing/stripe",
    tags=["billing"],
    dependencies=[Depends(_auth.require_admin)],
)


class CheckoutSessionRequest(BaseModel):
    price_id: str = Field(default="", description="Stripe Price ID override")
    success_url: str = Field(default="", description="Checkout success URL override")
    cancel_url: str = Field(default="", description="Checkout cancel URL override")
    customer_id: str = Field(default="", description="Existing Stripe customer ID")


class BillingPortalSessionRequest(BaseModel):
    customer_id: str = Field(..., description="Stripe customer ID")
    return_url: str = Field(default="", description="Portal return URL override")


@router.post("/checkout-session")
async def create_stripe_checkout_session(
    body: CheckoutSessionRequest,
    user: _auth.User = Depends(_auth.current_user),
) -> dict[str, str]:
    config = StripeBillingConfig.from_settings(settings)
    try:
        session = await create_checkout_session(
            config,
            tenant_id=user.tenant_id,
            user_id=user.id,
            user_email=user.email,
            price_id=body.price_id,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            customer_id=body.customer_id,
        )
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StripeBillingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return session_response(session)


@router.post("/portal-session")
async def create_stripe_billing_portal_session(
    body: BillingPortalSessionRequest,
) -> dict[str, str]:
    config = StripeBillingConfig.from_settings(settings)
    try:
        session = await create_billing_portal_session(
            config,
            customer_id=body.customer_id,
            return_url=body.return_url,
        )
    except StripeBillingConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StripeBillingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return session_response(session)
