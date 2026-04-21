from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    meta_connections = db.relationship("MetaConnection", backref="user", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class MetaConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    fb_user_id = db.Column(db.String(50))
    fb_user_name = db.Column(db.String(100))
    access_token = db.Column(db.Text, nullable=False)
    token_expires = db.Column(db.DateTime, nullable=True)
    ad_account_id = db.Column(db.String(50))
    ad_account_name = db.Column(db.String(200))
    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_synced = db.Column(db.DateTime, nullable=True)

    def is_valid(self):
        if not self.token_expires:
            return True
        return datetime.utcnow() < self.token_expires


class CampaignCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    connection_id = db.Column(db.Integer, db.ForeignKey("meta_connection.id"), nullable=False)
    campaign_id = db.Column(db.String(50), nullable=False)
    campaign_name = db.Column(db.String(200))
    status = db.Column(db.String(30))
    objective = db.Column(db.String(50))
    spend = db.Column(db.Float, default=0)
    impressions = db.Column(db.Integer, default=0)
    clicks = db.Column(db.Integer, default=0)
    reach = db.Column(db.Integer, default=0)
    conversions = db.Column(db.Integer, default=0)
    ctr = db.Column(db.Float, default=0)
    cpc = db.Column(db.Float, default=0)
    cpm = db.Column(db.Float, default=0)
    roas = db.Column(db.Float, default=0)
    date_start = db.Column(db.String(20))
    date_stop = db.Column(db.String(20))
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)
    connection = db.relationship("MetaConnection", backref="campaigns")


class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used = db.Column(db.Boolean, default=False)
    user = db.relationship("User", backref="reset_tokens")

    def is_expired(self):
        from datetime import timedelta
        return datetime.utcnow() > self.created_at + timedelta(hours=24)


class AnalysisReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    connection_id = db.Column(db.Integer, db.ForeignKey("meta_connection.id"), nullable=False)
    report_type = db.Column(db.String(50), default="full")
    summary = db.Column(db.Text)
    recommendations = db.Column(db.Text)
    score = db.Column(db.Integer)
    date_range = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User", backref="reports")
    connection = db.relationship("MetaConnection", backref="reports")
