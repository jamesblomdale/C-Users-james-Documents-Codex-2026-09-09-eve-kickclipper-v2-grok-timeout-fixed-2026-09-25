"""
SQLAlchemy models for the SaaS layer: accounts, the credit ledger, and
job/clip tracking. This is the source of truth for "how many clips did
job X actually produce" -- the UI must always read clip counts from the
`Clip` table, never from a log line, which is the exact bug class
("says 31 clips, library is empty") this replaces.

Design notes:
- Credits are a float "processed source minutes" unit, not cents --
  keeps the ledger decoupled from whatever price-per-credit gets set
  later in Stripe.
- CreditTransaction is an append-only log. `CreditAccount.balance` and
  `.held` are maintained fields (not recomputed from the log on every
  read) for speed, but every mutation to them MUST go through
  billing.py, which writes the matching transaction row in the same
  commit -- never touch balance/held directly elsewhere.
- Job.stage is the fine-grained pipeline stage (mirrors the existing
  "PROGRESS <stage> <pct>" lines main.py/vod.py already print).
  Job.status is the coarse outcome: queued/running/completed/
  completed_empty/failed/partial. A job is never "completed" unless
  clips were actually indexed -- see billing.finalize_job().
"""

import enum
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    OWNER = "owner"
    CUSTOMER = "customer"


class JobKind(str, enum.Enum):
    VOD = "vod"
    LIVE = "live"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    FAILED = "failed"
    PARTIAL = "partial"


class TxnType(str, enum.Enum):
    HOLD = "hold"
    CAPTURE = "capture"
    RELEASE = "release"
    REFUND = "refund"
    GRANT = "grant"
    PURCHASE = "purchase"
    ADJUST = "adjust"


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.CUSTOMER.value)
    created_at = db.Column(db.DateTime, default=utcnow)

    account = db.relationship("CreditAccount", backref="user", uselist=False)
    jobs = db.relationship("Job", backref="user")

    # Flask-Login expects these -- implemented directly rather than
    # inheriting UserMixin so it's obvious exactly what's here.
    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    @property
    def is_owner(self):
        return self.role == Role.OWNER.value


class CreditAccount(db.Model):
    __tablename__ = "credit_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    balance = db.Column(db.Float, nullable=False, default=0.0)   # available, not held
    held = db.Column(db.Float, nullable=False, default=0.0)      # reserved by in-flight jobs
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    transactions = db.relationship("CreditTransaction", backref="account")

    @property
    def available(self):
        """What can actually be spent right now -- balance already excludes held amounts."""
        return self.balance


class CreditTransaction(db.Model):
    __tablename__ = "credit_transactions"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("credit_accounts.id"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=True)
    type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    note = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=utcnow)


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    kind = db.Column(db.String(10), nullable=False)  # vod | live
    channel = db.Column(db.String(120), nullable=False)
    input_raw = db.Column(db.String(1000), nullable=False)
    preset_json = db.Column(db.Text, default="{}")

    stage = db.Column(db.String(40), default="queued")
    stage_pct = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default=JobStatus.QUEUED.value)
    error_message = db.Column(db.Text, default="")
    error_category = db.Column(db.String(40), default="")  # set by the incident classifier

    estimated_minutes = db.Column(db.Float, default=0.0)
    minutes_processed = db.Column(db.Float, default=0.0)
    credit_rate = db.Column(db.Float, default=1.0)
    hold_txn_id = db.Column(db.Integer, db.ForeignKey("credit_transactions.id"), nullable=True)

    output_dir = db.Column(db.String(500), default="")
    pid = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    clips = db.relationship("Clip", backref="job")
    events = db.relationship("JobEvent", backref="job", order_by="JobEvent.id")

    @property
    def kept_clips(self):
        return len(self.clips)


class Clip(db.Model):
    __tablename__ = "clips"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    subpath = db.Column(db.String(500), nullable=False)  # relative to OUTPUT_DIR, e.g. vod/N3on/clips/clip_000
    score = db.Column(db.Float, default=0.0)
    title = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=utcnow)


class JobEvent(db.Model):
    """Persisted log lines -- so a job's history survives an app restart,
    unlike the old in-memory-only log_lines list."""
    __tablename__ = "job_events"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    line = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _bootstrap_owner(app)


def _bootstrap_owner(app):
    """Creates the first owner account from OWNER_EMAIL/OWNER_PASSWORD in
    .env if no users exist yet. There's no signup-as-owner flow by
    design -- ownership is a deployment-time decision, not a form."""
    from config import Config
    from werkzeug.security import generate_password_hash

    if User.query.count() > 0:
        return
    if not Config.OWNER_EMAIL or not Config.OWNER_PASSWORD:
        app.logger.warning(
            "No users exist and OWNER_EMAIL/OWNER_PASSWORD aren't set in .env -- "
            "no one can log in yet. Set them and restart."
        )
        return
    owner = User(
        email=Config.OWNER_EMAIL.lower().strip(),
        password_hash=generate_password_hash(Config.OWNER_PASSWORD),
        role=Role.OWNER.value,
    )
    db.session.add(owner)
    db.session.flush()
    db.session.add(CreditAccount(user_id=owner.id, balance=999999.0, held=0.0))
    db.session.commit()
    app.logger.info(f"Created owner account for {owner.email}")
