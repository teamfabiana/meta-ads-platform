import requests
from datetime import datetime, timedelta
from models import db, MetaConnection, CampaignCache

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


def get_oauth_url(app_id, redirect_uri):
    scope = "ads_read,ads_management,business_management,public_profile"
    return (
        f"https://www.facebook.com/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&response_type=code"
    )


def exchange_code_for_token(app_id, app_secret, code, redirect_uri):
    resp = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_long_lived_token(app_id, app_secret, short_token):
    resp = requests.get(
        f"{GRAPH_API_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    expires_in = data.get("expires_in", 5184000)  # default 60 days
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    return data.get("access_token"), expires_at


def get_fb_user(access_token):
    resp = requests.get(
        f"{GRAPH_API_BASE}/me",
        params={"fields": "id,name,email", "access_token": access_token},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_ad_accounts(access_token):
    resp = requests.get(
        f"{GRAPH_API_BASE}/me/adaccounts",
        params={
            "fields": "id,name,account_status,currency,timezone_name",
            "access_token": access_token,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_campaigns(access_token, ad_account_id, date_preset="last_30d"):
    account_id = ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
    fields = (
        "id,name,status,objective,"
        "insights.date_preset({preset}){{"
        "spend,impressions,clicks,reach,actions,"
        "ctr,cpc,cpm,action_values"
        "}}"
    ).format(preset=date_preset)

    resp = requests.get(
        f"{GRAPH_API_BASE}/{account_id}/campaigns",
        params={"fields": fields, "access_token": access_token, "limit": 50},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def parse_action_value(insights, action_type, value_field="value"):
    actions = insights.get("actions", [])
    for action in actions:
        if action.get("action_type") == action_type:
            return float(action.get(value_field, 0))
    return 0.0


def sync_campaigns(connection: MetaConnection, date_preset="last_30d"):
    """Fetch campaigns from Meta API and upsert into cache."""
    raw_campaigns = fetch_campaigns(
        connection.access_token, connection.ad_account_id, date_preset
    )

    # Remove stale cache for this connection
    CampaignCache.query.filter_by(connection_id=connection.id).delete()

    for camp in raw_campaigns:
        insights = {}
        if "insights" in camp and "data" in camp["insights"] and camp["insights"]["data"]:
            insights = camp["insights"]["data"][0]

        spend = float(insights.get("spend", 0))
        impressions = int(insights.get("impressions", 0))
        clicks = int(insights.get("clicks", 0))
        reach = int(insights.get("reach", 0))
        conversions = int(parse_action_value(insights, "purchase") or
                         parse_action_value(insights, "lead") or
                         parse_action_value(insights, "complete_registration"))
        ctr = float(insights.get("ctr", 0))
        cpc = float(insights.get("cpc", 0))
        cpm = float(insights.get("cpm", 0))

        revenue = parse_action_value(insights, "purchase", "value")
        roas = (revenue / spend) if spend > 0 else 0

        record = CampaignCache(
            connection_id=connection.id,
            campaign_id=camp["id"],
            campaign_name=camp.get("name", ""),
            status=camp.get("status", ""),
            objective=camp.get("objective", ""),
            spend=spend,
            impressions=impressions,
            clicks=clicks,
            reach=reach,
            conversions=conversions,
            ctr=ctr,
            cpc=cpc,
            cpm=cpm,
            roas=roas,
            date_start=insights.get("date_start", ""),
            date_stop=insights.get("date_stop", ""),
        )
        db.session.add(record)

    connection.last_synced = datetime.utcnow()
    db.session.commit()
    return len(raw_campaigns)
