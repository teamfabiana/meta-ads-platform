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

    benchmarks = """
INDUSTRY BENCHMARKS (2024) — use these to evaluate performance:

CTR (Click-Through Rate):
  - Cold traffic: 1.5–2.5%+ is good | Below 1% = creative is struggling
  - Warm traffic: 3–5%+ is good

CPC (Cost Per Click):
  - E-commerce: $0.80–$1.50 is normal
  - Info products / coaching: $1.00–$3.00 is normal
  - Below $0.50 = excellent | Above $3.00 = needs attention

CPM (Cost Per 1,000 Impressions):
  - Under $10 = great | $10–$25 = normal | Over $30 = audience fatigue or saturation

Landing Page CVR:
  - Cold traffic: 2–5% | Warm traffic: 5–10%+ | Optimized funnels: 10–20%+

ROAS (Return on Ad Spend):
  - Break-even: 2–3x (depends on margins)
  - Healthy: 3–5x | Scaling profitably: 4x+ blended

Hook Rate (3-sec video view rate):
  - Scroll-stopping: 40–50%+ | Decent: 30–40% | Needs work: Below 30%
"""

    prompt = f"""You are an expert Meta Ads (Facebook/Instagram Ads) strategist and analyst. Analyze the following campaign performance data for {date_range} and provide actionable insights.

Ad Account: {connection.ad_account_name}

{benchmarks}

ACCOUNT PERFORMANCE SUMMARY:
{json.dumps(summary_data, indent=2)}

INDIVIDUAL CAMPAIGN DETAILS (top 20 by spend):
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

Using the benchmarks above as your evaluation standard, please provide:
1. EXECUTIVE SUMMARY (2-3 paragraphs): Overall account health, key wins, key concerns — reference specific benchmark comparisons (e.g. "Your CTR of 0.8% is below the 1.5% cold traffic benchmark")
2. PERFORMANCE SCORE (0-100): Rate overall performance against the benchmarks with justification
3. TOP 5 RECOMMENDATIONS: Specific, prioritized actions. Each must include:
   - The issue (with benchmark comparison)
   - Exact action to take
   - Expected impact
4. BUDGET OPTIMIZATION: Which campaigns to scale, pause, or reallocate budget from/to
5. QUICK WINS: 2-3 things they can do TODAY to improve results

Format your response as JSON with these keys:
- summary (string, HTML formatted with <p>, <strong>, <ul>, <li> tags)
- score (integer 0-100)
- score_label (string: "Poor", "Fair", "Good", "Excellent")
- recommendations (array of objects with: title, issue, action, impact, priority ["high"/"medium"/"low"])
- budget_optimization (string, HTML formatted)
- quick_wins (array of strings)
- key_metrics_assessment (object with keys: ctr_performance, cpc_efficiency, cpm_health, roas_strength — each an object with: rating ["low"/"medium"/"high"], benchmark (string), actual (string), note (string))"""

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
