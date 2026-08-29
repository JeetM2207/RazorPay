import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


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
