"""
Credit-reservation invariants (cost-impacting — see conftest).

The upfront 0.4 reservation for a live apply must be refunded exactly once
on ANY non-success path, including the ones that are not `Exception`s
(asyncio.CancelledError from the APPLICATION_TIMEOUT watchdog / shutdown)
and the ones that run from a different process (boot sweep, zombie sweep,
GET /queue sweep).
"""
import inspect
import re


def _flat(src: str) -> str:
    return re.sub(r"\s+", " ", src)


def test_reserve_credit_is_one_transaction_with_conditional_deduct():
    import db
    src = _flat(inspect.getsource(db.reserve_credit))
    assert "async with conn.transaction()" in src
    assert "WHERE id = $2 AND COALESCE(credits, 0) >= $1" in src
    assert "credit_reserved" in src


def test_refund_reservation_is_idempotent_via_conditional_flag_flip():
    """Only the caller that flips credit_reserved TRUE->FALSE may add credits."""
    import db
    src = _flat(inspect.getsource(db.refund_reservation))
    assert "WHERE user_id = $1 AND job_id = $2 AND credit_reserved = TRUE" in src
    assert "RETURNING id" in src
    # The add-credits UPDATE must come AFTER the claim, inside the same txn.
    assert src.index("credit_reserved = TRUE") < src.index("credits = COALESCE(credits, 0) + $1")
    assert "async with conn.transaction()" in src


def test_run_application_refunds_on_cancelled_error():
    """asyncio.CancelledError is a BaseException — a plain `except Exception`
    never saw the wait_for timeout and the reservation was silently kept."""
    from api.routes import apply as apply_mod
    src = inspect.getsource(apply_mod.run_application)
    assert "except asyncio.CancelledError" in src
    cancel_block = src.split("except asyncio.CancelledError:")[1].split("except Exception as e:")[0]
    assert "refund_reservation" in cancel_block
    assert "raise" in cancel_block  # must re-raise so wait_for still times out
    # Success path consumes (never refunds) the reservation.
    assert "consume_reservation" in src


def test_queue_timeout_handler_refunds():
    from api.routes import queue as queue_mod
    src = inspect.getsource(queue_mod.process_user_queue)
    timeout_block = src.split("except asyncio.TimeoutError:")[1].split("except Exception as e:")[0]
    assert "refund_reservation" in timeout_block


def test_init_db_no_longer_blanket_fails_applying_rows():
    """The old reset ran from the cron worker too and killed live applies
    (without refunding). Stuck rows go through the age-gated sweep now."""
    import db
    src = _flat(inspect.getsource(db.init_db))
    assert "SET status = 'failed', notes = 'Server restarted during apply'" not in src
    assert "credit_reserved BOOLEAN" in src


def test_fail_stuck_applications_is_age_gated_and_refund_aware():
    import db
    src = _flat(inspect.getsource(db.fail_stuck_applications))
    assert "WHERE status = 'applying'" in src
    assert "applied_at < NOW() - make_interval(mins => $2::int)" in src
    assert "refund_reservation" in src
    assert inspect.signature(db.fail_stuck_applications).parameters["older_than_minutes"].default == 15


def test_every_stuck_row_sweep_goes_through_the_shared_helper():
    """GET /queue used to mark 'applying' rows failed WITHOUT refunding —
    and because the dashboard polls it every few seconds it beat the
    refund-aware zombie sweep nearly every time."""
    from api.routes import queue as queue_mod
    import scheduler
    assert "fail_stuck_applications" in inspect.getsource(queue_mod.get_queue)
    assert "fail_stuck_applications" in inspect.getsource(scheduler._sweep_zombie_applications)
    assert "SET status = 'failed'" not in inspect.getsource(queue_mod.get_queue)
    from api import main as api_main
    assert "fail_stuck_applications" in inspect.getsource(api_main.lifespan)
