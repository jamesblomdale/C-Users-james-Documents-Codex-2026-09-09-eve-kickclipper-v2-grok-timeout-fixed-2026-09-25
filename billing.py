"""
The credit ledger. Every balance/held mutation goes through here so a
CreditTransaction row is always written in the same commit as the
balance change -- the transaction log and the running totals can never
drift apart, which is the whole point of having a ledger instead of a
single integer.

Core lifecycle for a job:
    1. estimate_and_hold()  -- called once source duration is known
                                (after resolving_source), BEFORE the
                                expensive transcribe/score/render work
                                starts. Fails fast if the user doesn't
                                have enough available balance.
    2. finalize_job()       -- called once, when the job's pipeline
                                subprocess exits (success or failure).
                                Reads the ACTUAL clip count from the
                                `Clip` table (never a log line) and:
                                  - 0 clips, pipeline ran clean
                                        -> completed_empty, full refund
                                  - 0 clips, pipeline crashed
                                        -> failed, full refund
                                  - >0 clips, pipeline ran clean
                                        -> completed, capture full hold
                                  - >0 clips, pipeline crashed partway
                                        -> partial, capture the hold
                                        (you already produced value;
                                        see note in finalize_job)

All amounts are in credits (1 credit = 1 processed source minute).
"""

import json
import math

from models import db, CreditAccount, CreditTransaction, Job, JobStatus, TxnType, utcnow


class InsufficientCredits(Exception):
    def __init__(self, available, needed):
        self.available = available
        self.needed = needed
        super().__init__(f"Need {needed:.1f} credits, only {available:.1f} available")


def get_or_create_account(user_id):
    acct = CreditAccount.query.filter_by(user_id=user_id).first()
    if not acct:
        acct = CreditAccount(user_id=user_id, balance=0.0, held=0.0)
        db.session.add(acct)
        db.session.commit()
    return acct


def _log_txn(account, job_id, txn_type, amount, note=""):
    txn = CreditTransaction(account_id=account.id, job_id=job_id, type=txn_type, amount=amount, note=note)
    db.session.add(txn)
    db.session.flush()  # get txn.id without committing yet
    return txn


def estimate_and_hold(job: Job, source_minutes: float):
    """
    Places a credit hold sized to the job's actual source duration (now
    known, after a cheap resolve+probe -- not a wild guess made before
    we knew anything about the file). Raises InsufficientCredits (and
    leaves the job untouched) if the user can't cover it; the caller is
    expected to mark the job failed with a clear reason in that case.
    """
    account = get_or_create_account(job.user_id)
    estimated = math.ceil(source_minutes * job.credit_rate * 10) / 10  # round up to 0.1 credit
    if account.balance < estimated:
        raise InsufficientCredits(account.balance, estimated)

    account.balance -= estimated
    account.held += estimated
    txn = _log_txn(account, job.id, TxnType.HOLD.value, estimated,
                    note=f"hold for job {job.id}: {source_minutes:.1f} source min @ {job.credit_rate}x")
    job.estimated_minutes = source_minutes
    job.hold_txn_id = txn.id
    db.session.commit()
    return estimated


def release_unused(job: Job, keep_amount: float):
    """
    Releases whatever part of the hold isn't being captured, back to
    available balance. keep_amount is what capture_hold() is about to
    take -- this returns (hold_amount - keep_amount) that's left over.
    """
    hold_txn = db.session.get(CreditTransaction, job.hold_txn_id) if job.hold_txn_id else None
    if not hold_txn:
        return 0.0
    account = db.session.get(CreditAccount, hold_txn.account_id)
    held_amount = hold_txn.amount
    to_release = max(0.0, held_amount - keep_amount)
    if to_release > 0:
        account.held -= to_release
        account.balance += to_release
        _log_txn(account, job.id, TxnType.RELEASE.value, to_release,
                 note=f"released unused hold for job {job.id}")
    return to_release


def capture(job: Job, amount: float, note=""):
    hold_txn = db.session.get(CreditTransaction, job.hold_txn_id) if job.hold_txn_id else None
    account = db.session.get(CreditAccount, hold_txn.account_id) if hold_txn else get_or_create_account(job.user_id)
    amount = min(amount, account.held)  # never capture more than is actually held
    account.held -= amount
    _log_txn(account, job.id, TxnType.CAPTURE.value, amount, note=note or f"captured for job {job.id}")
    job.minutes_processed = amount / job.credit_rate if job.credit_rate else amount


def refund_full_hold(job: Job, reason: str):
    """Returns the ENTIRE outstanding hold for this job to available
    balance. Used for completed_empty and failed-due-to-our-pipeline."""
    hold_txn = db.session.get(CreditTransaction, job.hold_txn_id) if job.hold_txn_id else None
    if not hold_txn:
        return 0.0
    account = db.session.get(CreditAccount, hold_txn.account_id)
    amount = account.held
    # Only release what THIS job's hold actually contributed, not the
    # account's entire held total (other jobs may also have holds open).
    amount = min(amount, hold_txn.amount)
    if amount > 0:
        account.held -= amount
        account.balance += amount
        _log_txn(account, job.id, TxnType.REFUND.value, amount, note=f"refund for job {job.id}: {reason}")
    return amount


def grant(user_id, amount, note="manual grant"):
    account = get_or_create_account(user_id)
    account.balance += amount
    _log_txn(account, None, TxnType.GRANT.value, amount, note=note)
    db.session.commit()


PIPELINE_FAULT_CATEGORIES = {
    "kick_source", "quota", "llm_parse", "ffmpeg", "storage", "timeout", "unknown",
}
# transcript_empty is treated as a real (if disappointing) result, not
# a fault -- it means the pipeline worked and genuinely found nothing.
USER_FAULT_CATEGORIES = {"transcript_empty", "insufficient_credits", "bad_input"}


TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value, JobStatus.COMPLETED_EMPTY.value,
    JobStatus.FAILED.value, JobStatus.PARTIAL.value,
}


def finalize_job(job: Job, crashed: bool, error_category: str = "", capture_amount: float = None):
    """
    The single place a job's terminal status and credit settlement get
    decided. Meant to be called exactly once, when the subprocess exits,
    but guarded to be a no-op if it's ever called again on an
    already-terminal job (a retried webhook, a duplicate exit event) --
    without this, a second call would re-run capture/release math and
    could silently corrupt minutes_processed even though the account
    balance itself happens to stay correct. Reads clip count from the
    DB (job.kept_clips), never from log text.

    capture_amount overrides how much of the hold gets captured (the
    rest is released back to available balance) -- VOD leaves this
    None because its hold already equals the real source duration
    (there's nothing to correct for), but a live job's hold is a fixed
    pre-authorized block sized well above a typical session, sized that
    way because the session length isn't known upfront. Without this
    override, live would always capture the FULL block regardless of
    how long the session actually ran, silently breaking the "pay only
    for footage actually processed" promise for live specifically.
    """
    if job.status in TERMINAL_STATUSES:
        return
    kept = job.kept_clips

    if job.hold_txn_id is None:
        # Job never got as far as placing a hold (e.g. failed during
        # resolving_source, before duration was even known) -- nothing
        # to settle, just record the outcome.
        job.status = JobStatus.FAILED.value if crashed else JobStatus.COMPLETED_EMPTY.value
        job.error_category = error_category
        job.finished_at = utcnow()
        db.session.commit()
        return

    if kept == 0:
        refunded = refund_full_hold(
            job, "no keepable clips found" if not crashed else f"pipeline error: {error_category or 'unknown'}"
        )
        job.status = JobStatus.FAILED.value if crashed else JobStatus.COMPLETED_EMPTY.value
        job.error_category = error_category
        job.finished_at = utcnow()
        db.session.commit()
        return

    # At least one clip made it to the library -- real value was
    # delivered, so the full source-duration hold is captured even on a
    # partial crash (the expensive transcribe+score+render work already
    # happened for the whole file; see billing.py module docstring).
    # This is a simplification of a strict per-clip cost model, and is
    # exactly what the pricing page promises -- keep them in sync.
    hold_txn = db.session.get(CreditTransaction, job.hold_txn_id)
    to_capture = hold_txn.amount if capture_amount is None else min(capture_amount, hold_txn.amount)
    capture(job, to_capture, note=f"job {job.id}: {kept} clip(s) kept")
    release_unused(job, to_capture)  # returns any part of the hold not captured (0 for VOD's exact-cost hold)
    job.status = JobStatus.PARTIAL.value if crashed else JobStatus.COMPLETED.value
    job.error_category = error_category
    job.finished_at = utcnow()
    db.session.commit()


def account_summary(user_id):
    account = get_or_create_account(user_id)
    return {"balance": round(account.balance, 1), "held": round(account.held, 1)}
