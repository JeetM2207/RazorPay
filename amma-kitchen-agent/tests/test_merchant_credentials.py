"""One key per kitchen, and it opens exactly one board.

Before this the console had a kitchen dropdown and a single shared
password, which made the dropdown an invitation: anybody holding it
could sign in as any merchant on the platform, read their orders and
reprice their menu. The kitchen and the password are checked as a pair
now.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import merchant_auth

AMMA = "amma-test-key"
BOMBAY = "bombay-test-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-signing-key")
    monkeypatch.setenv("MERCHANT_CONSOLE_PASSWORD", AMMA)
    monkeypatch.setenv("MERCHANT_PASSWORD_BOMBAY_TIFFIN", BOMBAY)
    monkeypatch.delenv("MERCHANT_PASSWORD_LAHORI_GRILL", raising=False)
    monkeypatch.delenv("MERCHANT_PASSWORD_AMMAS_KITCHEN", raising=False)
    return TestClient(app_module.app, follow_redirects=False)


def login(client, password, merchant_id):
    return client.post("/merchant/login",
                       data={"password": password, "merchant_id": merchant_id})


# ------------------------------------------------------- the diagonal

def test_a_key_opens_its_own_kitchen(client):
    assert login(client, AMMA, "ammas-kitchen").status_code == 303
    assert login(client, BOMBAY, "bombay-tiffin").status_code == 303


def test_a_key_does_not_open_another_kitchen(client):
    """The one that mattered: the dropdown must not be a way in."""
    assert login(client, AMMA, "bombay-tiffin").status_code != 303
    assert login(client, BOMBAY, "ammas-kitchen").status_code != 303


def test_the_shared_password_is_only_the_default_kitchens(client):
    """MERCHANT_CONSOLE_PASSWORD stays the default kitchen's so demo.py,
    predemo_check.py and the buyer agents log in unchanged -- and it
    stops being a skeleton key for everybody else."""
    assert merchant_auth.password_is_correct(AMMA, "ammas-kitchen") is True
    assert merchant_auth.password_is_correct(AMMA, "bombay-tiffin") is False


def test_a_kitchen_with_no_key_cannot_be_signed_into(client):
    """Lahori has no password in this fixture. Nobody gets in -- which is
    the right failure, rather than falling back to a shared one."""
    assert merchant_auth.password_is_correct(AMMA, "lahori-grill") is False
    assert merchant_auth.password_is_correct("", "lahori-grill") is False
    assert login(client, AMMA, "lahori-grill").status_code != 303
    assert "lahori-grill" in merchant_auth.kitchens_without_a_password()


# ------------------------------------------- what the session then says

def test_the_session_names_the_kitchen_that_signed_in(client):
    resp = login(client, BOMBAY, "bombay-tiffin")
    cookie = resp.cookies.get(merchant_auth.COOKIE_NAME)
    assert merchant_auth.merchant_from_cookie(cookie) == "bombay-tiffin"


def test_a_kitchen_that_is_not_on_the_platform_is_refused(client):
    assert login(client, AMMA, "no-such-kitchen").status_code != 303


def test_no_password_is_ever_echoed_back(client):
    """A credential in a response is a credential in a log, a proxy and
    somebody's browser history."""
    body = login(client, "wrong-password", "bombay-tiffin").text
    assert "wrong-password" not in body
    assert BOMBAY not in body
    assert AMMA not in body
