import os
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

app = FastAPI(
    title="Google Ads AI Agent",
    version="1.0.0",
    description="Safe Google Ads reporting and controlled changes."
)

# Temporary in-memory confirmation plans.
# For production across multiple Cloud Run instances, move this to Firestore.
PENDING_PLANS: Dict[str, Dict[str, Any]] = {}
PLAN_TTL_SECONDS = 15 * 60


def require_api_key(x_api_key: Optional[str]) -> None:
    expected = os.environ.get("AGENT_API_KEY", "")
    if not expected or not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")


def ads_client() -> GoogleAdsClient:
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Missing server configuration: {', '.join(missing)}"
        )

    config = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "use_proto_plus": True,
    }
    login_customer_id = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_customer_id:
        config["login_customer_id"] = login_customer_id.replace("-", "")
    return GoogleAdsClient.load_from_dict(config)


def clean_customer_id(customer_id: str) -> str:
    value = customer_id.replace("-", "").strip()
    if not value.isdigit():
        raise HTTPException(status_code=400, detail="customer_id must contain digits only")
    return value


class StatsRequest(BaseModel):
    customer_id: str
    date_range: str = Field(
        default="LAST_30_DAYS",
        description="Google Ads date range such as TODAY, YESTERDAY, LAST_7_DAYS, LAST_30_DAYS"
    )


class ChangePlanRequest(BaseModel):
    customer_id: str
    action: str = Field(description="pause_campaign, enable_campaign, or set_campaign_budget")
    campaign_id: str
    new_daily_budget_cad: Optional[float] = None
    reason: str = ""


class ExecutePlanRequest(BaseModel):
    confirmation_token: str
    confirmed: bool


@app.get("/health")
def health():
    return {"status": "ok", "service": "google-ads-ai-agent"}


@app.get("/accounts")
def list_accessible_accounts(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    client = ads_client()
    service = client.get_service("CustomerService")
    response = service.list_accessible_customers()
    return {"resource_names": list(response.resource_names)}


@app.post("/campaigns/stats")
def campaign_stats(
    request: StatsRequest,
    x_api_key: Optional[str] = Header(default=None)
):
    require_api_key(x_api_key)
    client = ads_client()
    customer_id = clean_customer_id(request.customer_id)
    service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM campaign
        WHERE segments.date DURING {request.date_range}
        ORDER BY metrics.cost_micros DESC
    """

    rows = []
    try:
        stream = service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                rows.append({
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "status": row.campaign.status.name,
                    "impressions": int(row.metrics.impressions),
                    "clicks": int(row.metrics.clicks),
                    "cost_cad": round(row.metrics.cost_micros / 1_000_000, 2),
                    "conversions": float(row.metrics.conversions),
                })
    except GoogleAdsException as exc:
        raise HTTPException(status_code=400, detail=str(exc.failure))

    return {
        "customer_id": customer_id,
        "date_range": request.date_range,
        "campaigns": rows,
    }


@app.post("/changes/plan")
def plan_change(
    request: ChangePlanRequest,
    x_api_key: Optional[str] = Header(default=None)
):
    require_api_key(x_api_key)

    allowed = {"pause_campaign", "enable_campaign", "set_campaign_budget"}
    if request.action not in allowed:
        raise HTTPException(status_code=400, detail=f"Allowed actions: {sorted(allowed)}")
    if request.action == "set_campaign_budget":
        if request.new_daily_budget_cad is None or request.new_daily_budget_cad <= 0:
            raise HTTPException(status_code=400, detail="A positive new_daily_budget_cad is required")

    token = secrets.token_urlsafe(24)
    PENDING_PLANS[token] = {
        "created_at": time.time(),
        "request": request.model_dump(),
    }

    return {
        "requires_confirmation": True,
        "confirmation_token": token,
        "expires_in_seconds": PLAN_TTL_SECONDS,
        "summary": request.model_dump(),
        "instruction": "Show this exact plan to the user. Execute only after the user explicitly confirms it.",
    }


@app.post("/changes/execute")
def execute_change(
    request: ExecutePlanRequest,
    x_api_key: Optional[str] = Header(default=None)
):
    require_api_key(x_api_key)
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="The user did not confirm")

    plan = PENDING_PLANS.pop(request.confirmation_token, None)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found or already used")
    if time.time() - plan["created_at"] > PLAN_TTL_SECONDS:
        raise HTTPException(status_code=410, detail="Confirmation token expired")

    change = plan["request"]
    client = ads_client()
    customer_id = clean_customer_id(change["customer_id"])
    campaign_id = clean_customer_id(change["campaign_id"])

    if change["action"] in {"pause_campaign", "enable_campaign"}:
        campaign_service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = campaign_service.campaign_path(customer_id, campaign_id)
        enum = client.enums.CampaignStatusEnum
        campaign.status = enum.PAUSED if change["action"] == "pause_campaign" else enum.ENABLED
        client.copy_from(
            operation.update_mask,
            client.get_type("FieldMask")(paths=["status"])
        )
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[operation]
        )
        return {
            "executed": True,
            "action": change["action"],
            "resource_name": response.results[0].resource_name,
        }

    # Updating a campaign budget requires the campaign's budget resource name.
    google_ads_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT campaign.campaign_budget
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
    """
    result = google_ads_service.search(customer_id=customer_id, query=query)
    rows = list(result)
    if not rows:
        raise HTTPException(status_code=404, detail="Campaign not found")

    budget_resource_name = rows[0].campaign.campaign_budget
    budget_service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource_name
    budget.amount_micros = int(change["new_daily_budget_cad"] * 1_000_000)
    client.copy_from(
        operation.update_mask,
        client.get_type("FieldMask")(paths=["amount_micros"])
    )
    response = budget_service.mutate_campaign_budgets(
        customer_id=customer_id,
        operations=[operation]
    )
    return {
        "executed": True,
        "action": change["action"],
        "new_daily_budget_cad": change["new_daily_budget_cad"],
        "resource_name": response.results[0].resource_name,
    }
