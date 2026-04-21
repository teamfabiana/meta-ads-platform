import os
import json
import secrets
import string
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv

from models import db, User, MetaConnection, CampaignCache, AnalysisReport
from meta_api import (
    get_oauth_url, exchange_code_for_token, get_long_lived_token,
    get_fb_user, get_ad_accounts, sync_campaigns,
)
from analysis import generate_analysis, build_campaign_summary

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# Use PostgreSQL on Railway/cloud, SQLite locally
_db_url = os.environ.get("DATABASE_URL", "sqlite:///metainsights.db")
# Railway gives postgres:// but SQLAlchemy needs postgresql://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
REDIRECT_URI = f"{BASE_URL}/meta/callback"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("auth.html", mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or not password:
            flash("All fields are required.", "error")
            return render_template("auth.html", mode="register")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("auth.html", mode="register")
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("auth.html", mode="register")
        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        flash(f"Welcome, {name}! Connect your Meta Ads account to get started.", "success")
        return redirect(url_for("connect"))
    return render_template("auth.html", mode="register")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    campaigns = []
    stats = _empty_stats()
    top_campaigns = []
    campaigns_json = None

    if connection:
        campaigns = CampaignCache.query.filter_by(connection_id=connection.id).all()
        if campaigns:
            s = build_campaign_summary(campaigns)
            stats = _flatten_stats(s)
            top_campaigns = sorted(campaigns, key=lambda c: c.spend, reverse=True)[:10]
            campaigns_json = json.dumps([{
                "name": c.campaign_name, "spend": c.spend,
                "ctr": c.ctr, "roas": c.roas, "status": c.status,
            } for c in campaigns])

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        connection=connection,
        campaigns=campaigns,
        stats=stats,
        top_campaigns=top_campaigns,
        campaigns_json=campaigns_json,
    )


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.route("/campaigns")
@login_required
def campaigns():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    if not connection:
        flash("Connect your Meta Ads account first.", "info")
        return redirect(url_for("connect"))

    camps = CampaignCache.query.filter_by(connection_id=connection.id)\
        .order_by(CampaignCache.spend.desc()).all()

    active_count = sum(1 for c in camps if c.status == "ACTIVE")
    paused_count = sum(1 for c in camps if c.status == "PAUSED")
    total_spend = sum(c.spend for c in camps)
    avg_roas = (sum(c.roas for c in camps if c.roas > 0) /
                max(len([c for c in camps if c.roas > 0]), 1))

    return render_template(
        "campaigns.html",
        active_page="campaigns",
        connection=connection,
        campaigns=camps,
        active_count=active_count,
        paused_count=paused_count,
        total_spend=total_spend,
        avg_roas=avg_roas,
    )


# ── Analysis ──────────────────────────────────────────────────────────────────

@app.route("/analysis")
@login_required
def analysis():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    report_obj = None
    report = None

    if connection:
        report_obj = AnalysisReport.query.filter_by(
            user_id=current_user.id, connection_id=connection.id
        ).order_by(AnalysisReport.created_at.desc()).first()

        if report_obj:
            report = _parse_report(report_obj)

    return render_template(
        "analysis.html",
        active_page="analysis",
        connection=connection,
        report=report,
        enumerate=enumerate,
    )


@app.route("/analysis/generate", methods=["POST"])
@login_required
def generate_analysis_route():
    if not ANTHROPIC_API_KEY:
        flash("Anthropic API key not configured. Add ANTHROPIC_API_KEY to your .env file.", "error")
        return redirect(url_for("analysis"))

    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    if not connection:
        flash("Connect your Meta Ads account first.", "info")
        return redirect(url_for("connect"))

    campaigns = CampaignCache.query.filter_by(connection_id=connection.id).all()
    if not campaigns:
        flash("No campaign data found. Sync your account first.", "info")
        return redirect(url_for("connect"))

    try:
        result = generate_analysis(campaigns, connection, ANTHROPIC_API_KEY)
        report = AnalysisReport(
            user_id=current_user.id,
            connection_id=connection.id,
            summary=result.get("summary", ""),
            recommendations=json.dumps({
                "recommendations": result.get("recommendations", []),
                "quick_wins": result.get("quick_wins", []),
                "budget_optimization": result.get("budget_optimization", ""),
                "key_metrics_assessment": result.get("key_metrics_assessment", {}),
                "score_label": result.get("score_label", "Fair"),
            }),
            score=result.get("score", 0),
            date_range="Last 30 days",
        )
        db.session.add(report)
        db.session.commit()
        flash("Analysis generated successfully!", "success")
    except Exception as e:
        flash(f"Error generating analysis: {str(e)}", "error")

    return redirect(url_for("analysis"))


# ── Connect / OAuth ───────────────────────────────────────────────────────────

@app.route("/connect")
@login_required
def connect():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    oauth_url = get_oauth_url(META_APP_ID, REDIRECT_URI) if META_APP_ID else "#"
    return render_template(
        "connect.html",
        active_page="connect",
        connection=connection,
        oauth_url=oauth_url,
    )


@app.route("/meta/callback")
@login_required
def meta_callback():
    error = request.args.get("error")
    if error:
        flash(f"Facebook authorization failed: {request.args.get('error_description', error)}", "error")
        return redirect(url_for("connect"))

    code = request.args.get("code")
    if not code:
        flash("No authorization code received from Facebook.", "error")
        return redirect(url_for("connect"))

    try:
        token_data = exchange_code_for_token(META_APP_ID, META_APP_SECRET, code, REDIRECT_URI)
        short_token = token_data.get("access_token")
        long_token, expires_at = get_long_lived_token(META_APP_ID, META_APP_SECRET, short_token)
        fb_user = get_fb_user(long_token)
        ad_accounts = get_ad_accounts(long_token)

        if not ad_accounts:
            flash("No ad accounts found on this Facebook account.", "error")
            return redirect(url_for("connect"))

        # Store accounts in session for selection
        session["pending_token"] = long_token
        session["pending_expires"] = expires_at.isoformat()
        session["pending_fb_user"] = fb_user
        session["pending_ad_accounts"] = ad_accounts

        if len(ad_accounts) == 1:
            return redirect(url_for("select_account", account_id=ad_accounts[0]["id"]))

        return render_template("select_account.html", accounts=ad_accounts, active_page="connect")

    except Exception as e:
        flash(f"Error connecting Meta account: {str(e)}", "error")
        return redirect(url_for("connect"))


@app.route("/meta/select/<account_id>")
@login_required
def select_account(account_id):
    long_token = session.get("pending_token")
    expires_str = session.get("pending_expires")
    fb_user = session.get("pending_fb_user", {})
    ad_accounts = session.get("pending_ad_accounts", [])

    if not long_token:
        flash("Session expired. Please reconnect.", "error")
        return redirect(url_for("connect"))

    account = next((a for a in ad_accounts if a["id"] == account_id), None)
    if not account:
        flash("Account not found.", "error")
        return redirect(url_for("connect"))

    expires_at = datetime.fromisoformat(expires_str) if expires_str else None

    existing = MetaConnection.query.filter_by(user_id=current_user.id).first()
    if existing:
        existing.access_token = long_token
        existing.token_expires = expires_at
        existing.fb_user_id = fb_user.get("id")
        existing.fb_user_name = fb_user.get("name")
        existing.ad_account_id = account_id
        existing.ad_account_name = account.get("name", account_id)
        existing.connected_at = datetime.utcnow()
    else:
        conn = MetaConnection(
            user_id=current_user.id,
            access_token=long_token,
            token_expires=expires_at,
            fb_user_id=fb_user.get("id"),
            fb_user_name=fb_user.get("name"),
            ad_account_id=account_id,
            ad_account_name=account.get("name", account_id),
        )
        db.session.add(conn)

    db.session.commit()
    session.pop("pending_token", None)
    session.pop("pending_expires", None)
    session.pop("pending_fb_user", None)
    session.pop("pending_ad_accounts", None)

    flash("Meta Ads account connected! Syncing your campaigns…", "success")

    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    try:
        count = sync_campaigns(connection)
        flash(f"Synced {count} campaigns successfully.", "success")
    except Exception as e:
        flash(f"Account connected but sync failed: {str(e)}", "error")

    return redirect(url_for("dashboard"))


@app.route("/sync", methods=["POST"])
@login_required
def sync():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    if not connection:
        flash("No Meta Ads account connected.", "error")
        return redirect(url_for("connect"))
    try:
        count = sync_campaigns(connection)
        flash(f"Synced {count} campaigns successfully.", "success")
    except Exception as e:
        flash(f"Sync failed: {str(e)}", "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    connection = MetaConnection.query.filter_by(user_id=current_user.id).first()
    if connection:
        CampaignCache.query.filter_by(connection_id=connection.id).delete()
        AnalysisReport.query.filter_by(connection_id=connection.id).delete()
        db.session.delete(connection)
        db.session.commit()
        flash("Meta Ads account disconnected.", "success")
    return redirect(url_for("connect"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_stats():
    return type("Stats", (), {
        "total_spend": 0, "total_impressions": 0, "total_clicks": 0,
        "total_conversions": 0, "total_reach": 0,
        "avg_ctr": 0, "avg_cpc": 0, "avg_cpm": 0, "avg_roas": 0,
    })()


def _flatten_stats(s):
    return type("Stats", (), {
        "total_spend": s["totals"]["spend"],
        "total_impressions": s["totals"]["impressions"],
        "total_clicks": s["totals"]["clicks"],
        "total_conversions": s["totals"]["conversions"],
        "total_reach": s["totals"]["reach"],
        "avg_ctr": s["averages"]["ctr"],
        "avg_cpc": s["averages"]["cpc"],
        "avg_cpm": s["averages"]["cpm"],
        "avg_roas": s["averages"]["roas"],
    })()


def _parse_report(report_obj):
    try:
        payload = json.loads(report_obj.recommendations or "{}")
    except Exception:
        payload = {}

    # Support both old format (list) and new format (dict)
    if isinstance(payload, list):
        recs = payload
        quick_wins = []
        budget_opt = ""
        key_metrics = {}
        score_label = "Fair"
    else:
        recs = payload.get("recommendations", [])
        quick_wins = payload.get("quick_wins", [])
        budget_opt = payload.get("budget_optimization", "")
        key_metrics = payload.get("key_metrics_assessment", {})
        score_label = payload.get("score_label", "Fair")

    if not score_label:
        if report_obj.score >= 75:
            score_label = "Excellent"
        elif report_obj.score >= 60:
            score_label = "Good"
        elif report_obj.score >= 40:
            score_label = "Fair"
        else:
            score_label = "Poor"

    return type("Report", (), {
        "summary": report_obj.summary or "",
        "score": report_obj.score,
        "score_label": score_label,
        "recommendations": recs,
        "quick_wins": quick_wins,
        "budget_optimization": budget_opt,
        "key_metrics_assessment": key_metrics,
        "date_range": report_obj.date_range,
        "created_at": report_obj.created_at,
    })()


# ── Admin ─────────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    user_data = []
    for u in users:
        conn = MetaConnection.query.filter_by(user_id=u.id).first()
        report_count = AnalysisReport.query.filter_by(user_id=u.id).count()
        user_data.append({
            "user": u,
            "connection": conn,
            "report_count": report_count,
        })
    return render_template("admin.html", active_page="admin", user_data=user_data)


@app.route("/admin/reset/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin and user.id != current_user.id:
        flash("Cannot reset another admin's password.", "error")
        return redirect(url_for("admin_users"))

    # Generate a secure temporary password
    alphabet = string.ascii_letters + string.digits
    temp_password = "".join(secrets.choice(alphabet) for _ in range(12))
    user.set_password(temp_password)
    db.session.commit()
    flash(
        f"Password for {user.email} reset. Temporary password: {temp_password}",
        "success",
    )
    return redirect(url_for("admin_users"))


@app.route("/admin/delete/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_users"))
    if user.is_admin:
        flash("Cannot delete another admin account.", "error")
        return redirect(url_for("admin_users"))

    # Cascade delete all user data
    for conn in MetaConnection.query.filter_by(user_id=user.id).all():
        CampaignCache.query.filter_by(connection_id=conn.id).delete()
        AnalysisReport.query.filter_by(connection_id=conn.id).delete()
        db.session.delete(conn)
    AnalysisReport.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f"User {user.email} and all their data have been removed.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/toggle-admin/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot change your own admin status.", "error")
        return redirect(url_for("admin_users"))
    user.is_admin = not user.is_admin
    db.session.commit()
    status = "granted admin" if user.is_admin else "revoked admin from"
    flash(f"Successfully {status} {user.email}.", "success")
    return redirect(url_for("admin_users"))


# ── CLI helpers ───────────────────────────────────────────────────────────────

@app.cli.command("make-admin")
def make_admin_cmd():
    """Promote a user to admin by email. Usage: flask make-admin"""
    email = input("Email address to promote: ").strip().lower()
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if not user:
            print(f"No user found with email: {email}")
            return
        user.is_admin = True
        db.session.commit()
        print(f"✓ {user.name} ({email}) is now an admin.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=8080)
