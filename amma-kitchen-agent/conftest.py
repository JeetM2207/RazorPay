import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _isolate_audit_db(tmp_path, monkeypatch):
    """The suite must never write to the real audit trail.

    It did, for the whole life of this project, and the damage was
    invisible because the trail is append-only and nothing ever looked
    wrong: `agent-code`, `agent-0` through `agent-11`, `agent-A`,
    `auth-test` and fourteen hundred escalations were all pytest, sitting
    in the same database the consoles read. Every panel dutifully
    reported it. That is where "Rs.0 revenue, 171 interventions" came
    from -- not a bug in the panels, a suite writing into production.

    The nasty part is HOW the path is bound. `db_path: str =
    DEFAULT_DB_PATH` is evaluated once, when the function is defined, so
    monkeypatching the module attribute does nothing for the sixty
    functions that already captured it -- CLAUDE.md records the same trap
    biting get_events_for_agent. So the defaults are rewritten too, on
    every function that captured the old value.
    """
    import audit_log

    real = audit_log.DEFAULT_DB_PATH
    # Deliberately NOT "audit.db": tests that build their own database in
    # tmp_path use that name, and this fixture would have created it
    # first, so they failed with "table audit_events already exists".
    sandbox = str(tmp_path / "_suite_audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", sandbox)

    # Rebind every already-captured default. Done by value rather than by
    # name so a function taking the path under any argument name is still
    # covered, and so this keeps working if one is added later.
    for module_name in ("audit_log", "idempotency", "trust", "evidence",
                        "velocity", "mcp_orders", "routines", "orchestrator"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        for obj in vars(module).values():
            defaults = getattr(obj, "__defaults__", None)
            if not defaults or real not in defaults:
                continue
            obj.__defaults__ = tuple(
                sandbox if d == real else d for d in defaults)

    audit_log.init_db(sandbox)
    yield


@pytest.fixture(autouse=True)
def _isolate_merchant_config(tmp_path, monkeypatch):
    """Every test starts from the shipped defaults.

    merchant_config persists the shop to a JSON file, so without this a
    developer's saved menu would leak into the suite and tests would pass
    or fail depending on what was last configured in the browser.
    """
    import merchant_config

    monkeypatch.setattr(merchant_config, "CONFIG_PATH", tmp_path / "merchant_config.json")
    merchant_config.reset_to_defaults()

    # Her real default is 6 orders an hour, which is right for a kitchen
    # and wrong for a suite that fires twenty carts through one agent id
    # to test something else. The window is opened wide here so those
    # tests are testing what they mean to; tests/test_velocity.py sets
    # its own limits and is the one that exercises the real numbers.
    #
    # Deliberately widened rather than switched off: the gate still runs
    # on every order in every test, it simply has room. A bypass flag
    # would mean the suite never touched the code path at all.
    import velocity

    monkeypatch.setattr(
        velocity, "default_limits",
        lambda: velocity.VelocityLimits(max_orders_per_hour=10_000,
                                        max_spend_per_day_inr=100_000_000),
    )
    merchant_config.reset_to_defaults()
    yield
    merchant_config.reset_to_defaults()


@pytest.fixture(autouse=True)
def _isolate_reply_codes():
    """Every test starts with an empty rate-limit ledger.

    reply_codes counts failed code attempts per sender in a module-level
    dict, so without this a test that deliberately sends a wrong code
    pushes the NEXT test past the limit -- and it fails with the
    stonewall message instead of the re-ask, for reasons entirely
    invisible in its own body. Caught exactly that way.
    """
    import reply_codes

    reply_codes.reset()
    yield
    reply_codes.reset()


@pytest.fixture(autouse=True)
def _no_background_scheduler(monkeypatch):
    """The suite must never race a background task.

    Set before app.py's lifespan reads it, so a TestClient that starts the
    app starts no ticker. The scheduler's own tests call `scheduler.tick()`
    directly, which is the honest way to test it anyway -- driving a real
    60-second loop from a test would be testing asyncio, not this.
    """
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    yield
