import json
import anthropic
from models import CampaignCache, MetaConnection


def build_campaign_summary(campaigns: list[CampaignCache]) -> dict:
    total_spend = sum(c.spend for c in campaigns)
    total_impressions = sum(c.impressions for c in campaigns)
    total_clicks = sum(c.clicks for c in campaigns)
    total_conversions = sum(c.conversions for c in campaigns)
    total_reach = sum(c.reach for c in campaigns)

    avg_ctr = (total_clicks / total_impressions * 100) if total_impressions else 0
    avg_cpc = (total_spend / total_clicks) if total_clicks else 0
    avg_cpm = (total_spend / total_impressions * 1000) if total_impressions else 0
    avg_roas = sum(c.roas for c in campaigns if c.roas > 0) / max(
        len([c for c in campaigns if c.roas > 0]), 1
    )

    active = [c for c in campaigns if c.status == "ACTIVE"]
    paused = [c for c in campaigns if c.status == "PAUSED"]

    by_objective: dict = {}
    for c in campaigns:
        obj = c.objective or "UNKNOWN"
        if obj not in by_objective:
            by_objective[obj] = {"count": 0, "spend": 0, "clicks": 0, "conversions": 0}
        by_objective[obj]["count"] += 1
        by_objective[obj]["spend"] += c.spend
        by_objective[obj]["clicks"] += c.clicks
        by_objective[obj]["conversions"] += c.conversions

    top_spenders = sorted(campaigns, key=lambda c: c.spend, reverse=True)[:5]
    top_performers = sorted(
        [c for c in campaigns if c.roas > 0], key=lambda c: c.roas, reverse=True
    )[:5]
    underperformers = [
        c for c in campaigns
        if c.status == "ACTIVE" and c.spend > 0 and c.ctr < 0.5 and c.impressions > 1000
    ]

    return {
        "totals": {
            "spend": round(total_spend, 2),
            "impressions": total_impressions,
            "clicks": total_clicks,
            "conversions": total_conversions,
            "reach": total_reach,
        },
        "averages": {
            "ctr": round(avg_ctr, 2),
            "cpc": round(avg_cpc, 2),
            "cpm": round(avg_cpm, 2),
            "roas": round(avg_roas, 2),
        },
        "counts": {
            "total": len(campaigns),
            "active": len(active),
            "paused": len(paused),
        },
        "by_objective": by_objective,
        "top_spenders": [
            {"name": c.campaign_name, "spend": c.spend, "roas": c.roas, "ctr": c.ctr}
            for c in top_spenders
        ],
        "top_performers": [
            {"name": c.campaign_name, "roas": c.roas, "spend": c.spend}
            for c in top_performers
        ],
        "underperformers": [
            {"name": c.campaign_name, "ctr": c.ctr, "spend": c.spend, "status": c.status}
            for c in underperformers
        ],
    }


def generate_analysis(
    campaigns: list[CampaignCache],
    connection: MetaConnection,
    api_key: str,
    date_range: str = "Last 30 days",
) -> dict:
    if not campaigns:
        return {
            "summary": "No campaign data available to analyze.",
            "recommendations": "Connect your Meta Ads account and sync your campaigns to get AI-powered insights.",
            "score": 0,
        }

    summary_data = build_campaign_summary(campaigns)
    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are an expert Meta Ads (Facebook/Instagram Ads) analyst. Analyze the following campaign performance data for {date_range} and provide actionable insights.

Ad Account: {connection.ad_account_name}

PERFORMANCE SUMMARY:
{json.dumps(summary_data, indent=2)}

INDIVIDUAL CAMPAIGN DETAILS (top 20):
{json.dumps([
    {
        "name": c.campaign_name,
        "status": c.status,
        "objective": c.objective,
        "spend": c.spend,
        "impressions": c.impressions,
        "clicks": c.clicks,
        "ctr": c.ctr,
        "cpc": c.cpc,
        "cpm": c.cpm,
        "roas": c.roas,
        "conversions": c.conversions,
    }
    for c in sorted(campaigns, key=lambda x: x.spend, reverse=True)[:20]
], indent=2)}

Please provide:
1. EXECUTIVE SUMMARY (2-3 paragraphs): Overall account health, key wins, key concerns
2. PERFORMANCE SCORE (0-100): Rate the overall account performance with brief justification
3. TOP 5 RECOMMENDATIONS: Specific, actionable steps to improve performance. Each recommendation should include:
   - The issue or opportunity
   - Specific action to take
   - Expected impact
4. BUDGET OPTIMIZATION: How to reallocate budget for better results
5. QUICK WINS: 2-3 things they can do immediately (today) to improve results

Format your response as JSON with these keys:
- summary (string, HTML formatted with <p>, <strong>, <ul>, <li> tags)
- score (integer 0-100)
- score_label (string: "Poor", "Fair", "Good", "Excellent")
- recommendations (array of objects with: title, issue, action, impact, priority)
- budget_optimization (string, HTML formatted)
- quick_wins (array of strings)
- key_metrics_assessment (object with: spend_efficiency, audience_targeting, creative_performance, conversion_optimization — each rated "low/medium/high" with a note)"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "summary": f"<p>{raw}</p>",
            "score": 50,
            "score_label": "Fair",
            "recommendations": [],
            "budget_optimization": "",
            "quick_wins": [],
            "key_metrics_assessment": {},
        }

    result["summary_data"] = summary_data
    return result
