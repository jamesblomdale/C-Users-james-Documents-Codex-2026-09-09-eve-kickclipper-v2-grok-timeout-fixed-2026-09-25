"""
Email/password auth via Flask-Login. Google OAuth isn't implemented --
that needs a registered OAuth client (a real decision for whoever runs
this, not something to fake) so it's a clearly-flagged gap, not a demo.

Signup always creates a `customer`; the `owner` role only ever comes
from the OWNER_EMAIL/OWNER_PASSWORD bootstrap in models.py. There is no
UI path to become an owner -- that is deliberate.
"""

from functools import wraps
import os
from urllib.parse import urlsplit

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, User, CreditAccount, Role
from config import Config

login_manager = LoginManager()
login_manager.login_view = "auth.login"

auth_bp = Blueprint("auth", __name__)


def init_auth(app):
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    @app.before_request
    def demo_entry():
        if local_demo_request() and request.path in ('/login', '/signup', '/logout'):
            return redirect('/dashboard')
    app.context_processor(lambda: {'local_demo': local_demo_request()})


def local_demo_request():
    return (os.getenv('DEMO_MODE', '1') == '1'
            and request.remote_addr in ('127.0.0.1', '::1')
            and request.host.split(':')[0] in ('localhost', '127.0.0.1')
            and (not request.headers.get('Origin') or
                 urlsplit(request.headers['Origin']).netloc == request.host))


@login_manager.request_loader
def load_demo_user(req):
    # Request-scoped access: no reusable owner login cookie is issued.
    if local_demo_request():
        return User.query.filter_by(role=Role.OWNER.value).first()
    return None


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_owner:
            return "Forbidden -- owner only.", 403
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or "@" not in email or len(password) < 8:
        flash("Enter a valid email and a password of at least 8 characters.")
        return render_template("signup.html"), 400
    if User.query.filter_by(email=email).first():
        flash("An account with that email already exists.")
        return render_template("signup.html"), 400

    user = User(email=email, password_hash=generate_password_hash(password), role=Role.CUSTOMER.value)
    db.session.add(user)
    db.session.flush()
    # New accounts start with a small free trial's worth of credits --
    # matches the configured free plan (default 180 source minutes / 3 hours).
    db.session.add(CreditAccount(user_id=user.id, balance=Config.FREE_TRIAL_CREDITS, held=0.0))
    db.session.commit()
    login_user(user)
    return redirect(url_for("index"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        flash("Wrong email or password.")
        return render_template("login.html"), 401

    login_user(user)
    return redirect(url_for("index"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("landing"))
