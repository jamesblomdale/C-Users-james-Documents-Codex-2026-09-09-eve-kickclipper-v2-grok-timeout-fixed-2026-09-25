"""
Tests for the credit ledger and job finalization -- the part of this
app where a bug means a real chargeback, not just a wrong pixel. Run
with:
    python -m unittest tests.test_billing -v
from the project root (needs the venv active, or run with the venv's
python directly).

Uses an in-memory SQLite DB per test via Flask's app context, so these
never touch kickclipper.db.
"""

import unittest

from flask import Flask

from models import db, User, CreditAccount, Job, JobKind, JobStatus, Clip, Role
import billing


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


class BillingTestCase(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = User(email="clipper@example.com", password_hash="x", role=Role.CUSTOMER.value)
        db.session.add(self.user)
        db.session.flush()
        self.account = CreditAccount(user_id=self.user.id, balance=100.0, held=0.0)
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def make_job(self, credit_rate=1.0):
        job = Job(
            user_id=self.user.id, kind=JobKind.VOD.value, channel="teststreamer",
            input_raw="https://kick.com/teststreamer", credit_rate=credit_rate,
            status=JobStatus.QUEUED.value, stage="resolving_source",
        )
        db.session.add(job)
        db.session.commit()
        return job

    # ---------- hold / capture / release / refund arithmetic ----------

    def test_demo_zero_rate_accepts_long_vod_without_balance(self):
        self.account.balance = 0
        db.session.commit()
        job = self.make_job(credit_rate=0)
        self.assertEqual(billing.estimate_and_hold(job, source_minutes=480), 0)
        self.assertEqual(self.account.balance, 0)
        self.assertEqual(self.account.held, 0)
        self.assertIsNotNone(job.hold_txn_id)

    def test_hold_moves_balance_to_held(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=20.0)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 80.0)
        self.assertAlmostEqual(acct.held, 20.0)
        self.assertEqual(job.estimated_minutes, 20.0)
        self.assertIsNotNone(job.hold_txn_id)

    def test_hold_rounds_up_to_nearest_tenth_credit(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=10.03)  # 10.03 credits -> rounds up to 10.1
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.held, 10.1)

    def test_insufficient_credits_raises_and_touches_nothing(self):
        job = self.make_job()
        with self.assertRaises(billing.InsufficientCredits) as ctx:
            billing.estimate_and_hold(job, source_minutes=200.0)  # needs 200, only have 100
        self.assertEqual(ctx.exception.available, 100.0)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 100.0)  # untouched
        self.assertAlmostEqual(acct.held, 0.0)
        self.assertIsNone(job.hold_txn_id)

    def test_live_rate_charges_more_per_minute_than_vod(self):
        vod_job = self.make_job(credit_rate=1.0)
        billing.estimate_and_hold(vod_job, source_minutes=10.0)
        acct = CreditAccount.query.get(self.account.id)
        vod_hold = acct.held

        live_job = self.make_job(credit_rate=1.5)
        billing.estimate_and_hold(live_job, source_minutes=10.0)
        acct = CreditAccount.query.get(self.account.id)
        live_hold = acct.held - vod_hold
        self.assertGreater(live_hold, vod_hold)

    def test_refund_full_hold_returns_exactly_the_hold_amount(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=15.0)
        refunded = billing.refund_full_hold(job, "test refund")
        self.assertAlmostEqual(refunded, 15.0)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 100.0)  # back to where it started
        self.assertAlmostEqual(acct.held, 0.0)

    def test_capture_moves_from_held_not_from_balance(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=15.0)
        billing.capture(job, 15.0, note="test capture")
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 85.0)  # unchanged since the hold
        self.assertAlmostEqual(acct.held, 0.0)

    def test_capture_never_exceeds_what_is_actually_held(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=10.0)
        billing.capture(job, 999.0)  # try to overcharge
        acct = CreditAccount.query.get(self.account.id)
        self.assertGreaterEqual(acct.held, 0.0)  # never goes negative

    # ---------- finalize_job: the actual outcome logic ----------

    def test_zero_clips_clean_run_is_completed_empty_and_fully_refunded(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=30.0)
        billing.finalize_job(job, crashed=False)
        self.assertEqual(job.status, JobStatus.COMPLETED_EMPTY.value)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 100.0)
        self.assertAlmostEqual(acct.held, 0.0)

    def test_zero_clips_crashed_run_is_failed_and_fully_refunded(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=30.0)
        billing.finalize_job(job, crashed=True, error_category="ffmpeg")
        self.assertEqual(job.status, JobStatus.FAILED.value)
        self.assertEqual(job.error_category, "ffmpeg")
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 100.0)  # a fault on our end never costs the user

    def test_clips_produced_clean_run_is_completed_and_captured(self):
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=30.0)
        db.session.add(Clip(job_id=job.id, subpath="vod/teststreamer/clips/clip_000", score=75.0, title="test clip"))
        db.session.commit()
        billing.finalize_job(job, crashed=False)
        self.assertEqual(job.status, JobStatus.COMPLETED.value)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 70.0)  # 100 - 30 captured
        self.assertAlmostEqual(acct.held, 0.0)

    def test_live_job_only_captures_actual_elapsed_not_the_full_block(self):
        """Live pre-authorizes a fixed block (session length isn't known
        upfront), but must only be CHARGED for minutes actually run --
        a 5-minute test session must not cost the full pre-authorized
        hour. This is the exact mechanism app.py's _finalize relies on
        to compute real elapsed wall-clock time for live jobs."""
        job = self.make_job(credit_rate=1.5)
        billing.estimate_and_hold(job, source_minutes=60.0)  # pre-authorized block: 90 credits
        db.session.add(Clip(job_id=job.id, subpath="live/teststreamer/clips/clip_000", score=70.0, title="x"))
        db.session.commit()

        actual_elapsed_minutes = 5.0
        billing.finalize_job(job, crashed=False, capture_amount=actual_elapsed_minutes * job.credit_rate)

        self.assertEqual(job.status, JobStatus.COMPLETED.value)
        acct = CreditAccount.query.get(self.account.id)
        # 100 - (5 min * 1.5 rate) = 92.5, NOT 100 - 90 = 10
        self.assertAlmostEqual(acct.balance, 92.5)
        self.assertAlmostEqual(acct.held, 0.0)

    def test_clips_produced_then_crash_is_partial_not_failed(self):
        """A job that made at least one real clip before dying gets
        `partial`, not `failed` -- the user already has something to
        show for it, and shouldn't have to fight for a refund on the
        part that worked."""
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=30.0)
        db.session.add(Clip(job_id=job.id, subpath="vod/teststreamer/clips/clip_000", score=80.0, title="test clip"))
        db.session.commit()
        billing.finalize_job(job, crashed=True, error_category="ffmpeg")
        self.assertEqual(job.status, JobStatus.PARTIAL.value)

    def test_job_never_finalized_twice_double_charges(self):
        """Guards the exact bug class this whole ledger exists to
        prevent: calling finalize_job twice must never double-capture
        or double-refund."""
        job = self.make_job()
        billing.estimate_and_hold(job, source_minutes=20.0)
        db.session.add(Clip(job_id=job.id, subpath="vod/teststreamer/clips/clip_000", score=70.0, title="x"))
        db.session.commit()
        billing.finalize_job(job, crashed=False)
        acct_after_first = CreditAccount.query.get(self.account.id).balance
        minutes_after_first = job.minutes_processed

        # Simulate an accidental double-call (e.g. a retried webhook).
        billing.finalize_job(job, crashed=False)
        acct_after_second = CreditAccount.query.get(self.account.id).balance
        self.assertAlmostEqual(acct_after_first, acct_after_second)
        self.assertAlmostEqual(job.minutes_processed, minutes_after_first)  # not corrupted to 0

    def test_job_with_no_hold_placed_never_touches_ledger(self):
        """A job that died before resolving_source finished (before any
        estimate was even known) has no hold_txn_id -- finalize_job must
        not crash or touch the ledger for it."""
        job = self.make_job()
        billing.finalize_job(job, crashed=True, error_category="kick_source")
        self.assertEqual(job.status, JobStatus.FAILED.value)
        acct = CreditAccount.query.get(self.account.id)
        self.assertAlmostEqual(acct.balance, 100.0)
        self.assertAlmostEqual(acct.held, 0.0)


if __name__ == "__main__":
    unittest.main()
