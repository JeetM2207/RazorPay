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
