"""A login in front of every merchant surface that writes.

The consoles had none. `/api/merchant/optimize-prices` reprices the shop,
the setup page sets the budget cap the decision core runs on, and the
accept/reject endpoints move money -- all reachable by anyone holding the
ngrok URL, which gets pasted into a public connector setting.

The most important test in this file is
`test_a_new_merchant_route_cannot_be_added_without_the_guard`. Everything
else here checks what is true today; that one is the only thing that
stops this regressing the next time an endpoint is added.
"""

import time

import pytest
from fastapi.testclient import TestClient

import merchant_auth

PASSWORD = "test-merchant-password"
SECRET = "test-secret-key-for-signing-sessions"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MERCHANT_CONSOLE_PASSWORD", PASSWORD)
    monkeypatch.setenv("SECRET_KEY", SECRET)
    import app
    return TestClient(app.app, follow_redirects=False)


@pytest.fixture
def merchant(client):
    """A client holding a real session, obtained the way a person does."""
    resp = client.post("/merchant/login", data={"password": PASSWORD, "next": "/merchant/orders"})
    assert resp.status_code == 303
    assert merchant_auth.COOKIE_NAME in client.cookies
    return client


# Every surface this task put behind the login, by the route that serves
# it. Kept as data so the guard test below can compare it against what the
# app actually declares.
PROTECTED = [
    ("GET", "/merchant"),
    ("GET", "/merchant/orders"),
    ("GET", "/api/merchant-config"),
    ("POST", "/api/merchant-config"),
    ("POST", "/api/merchant/optimize-prices"),
    ("GET", "/api/insights"),
    ("GET", "/api/disputes"),
    ("POST", "/api/orders/1/dispute"),
    ("GET", "/api/evidence/1"),
    ("GET", "/evidence/1"),
    ("GET", "/api/sms"),
]

# Public by design, or authenticated by something stronger than a cookie.
PUBLIC = [
    ("GET", "/"),
    ("GET", "/buyer"),
    ("GET", "/buyer/order"),
    ("GET", "/catalog"),
    ("GET", "/audit"),
    ("GET", "/api/menu"),
    ("GET", "/api/pending"),
    ("GET", "/api/events"),
    ("GET", "/api/demand"),
    ("GET", "/api/transactions"),
    ("GET", "/api/buyer-mandate-defaults"),
    ("GET", "/merchant/login"),
]


def _call(client, method, path, **kw):
    return client.request(method, path, **kw)


# ------------------------------------------------------ the door is shut

@pytest.mark.parametrize("method,path", PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
def test_a_protected_surface_refuses_an_anonymous_caller(client, method, path):
    resp = _call(client, method, path)
    assert resp.status_code in (401, 303), f"{method} {path} answered {resp.status_code}"
    if resp.status_code == 303:
        assert "/merchant/login" in resp.headers["location"]


@pytest.mark.parametrize("method,path", PUBLIC, ids=[f"{m} {p}" for m, p in PUBLIC])
def test_the_public_surfaces_stay_reachable(client, method, path):
    """Protecting these would break the things this project exists to
    show: an agent reading the catalog, a judge reading the trail, a
    customer ordering."""
    resp = _call(client, method, path)
    assert resp.status_code < 400, f"{method} {path} answered {resp.status_code}"


def test_the_mcp_endpoint_is_not_behind_the_login(client):
    """Putting a cookie in front of /mcp would break the Claude connector
    outright -- it is how somebody else's model reaches the shop."""
    resp = client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
    assert resp.status_code != 401


def test_the_signed_webhooks_are_not_behind_the_login(client):
    """These authenticate themselves, with a signature rather than a
    cookie -- see reply_auth.py. A 403 here is that check firing, which is
    the point; a 401 would mean the login had swallowed them."""
    for path in ("/webhook/sms-reply", "/webhooks/razorpay"):
        assert client.post(path, data={"Body": "1"}).status_code != 401


# ------------------------------------------------------- and it opens

@pytest.mark.parametrize("method,path", PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
def test_a_logged_in_merchant_gets_through(merchant, method, path):
    resp = _call(merchant, method, path)
    assert resp.status_code not in (401, 403), f"{method} {path} refused a logged-in merchant"
    if resp.status_code == 303:
        assert "/merchant/login" not in resp.headers.get("location", "")


def test_the_wrong_password_does_not_let_anyone_in(client):
    resp = client.post("/merchant/login", data={"password": "not-it"})
    assert merchant_auth.COOKIE_NAME not in client.cookies
    # The wording names the PAIR now, because on a platform "wrong
    # password" is ambiguous -- it may be the right key for a different
    # kitchen.
    assert "not the password for that kitchen" in resp.text


def test_an_unset_password_refuses_everything(client, monkeypatch):
    """Rather than falling open, which is the failure that matters."""
    monkeypatch.delenv("MERCHANT_CONSOLE_PASSWORD", raising=False)
    assert merchant_auth.password_is_correct("") is False
    assert merchant_auth.password_is_correct("anything") is False


# --------------------------------------------------------- the cookie

def test_a_forged_cookie_is_rejected(client):
    for forged in ("", "nonsense", "1:2:3", f"{int(time.time())}:{int(time.time())+9999}:deadbeef"):
        client.cookies.set(merchant_auth.COOKIE_NAME, forged)
        assert client.get("/api/merchant-config").status_code == 401, f"accepted {forged!r}"
        client.cookies.clear()


def test_an_expired_cookie_is_rejected(client):
    stale = merchant_auth.issue_cookie(now=time.time() - merchant_auth.SESSION_SECONDS - 60)
    client.cookies.set(merchant_auth.COOKIE_NAME, stale)
    assert client.get("/api/merchant-config").status_code == 401


def test_the_expiry_cannot_be_edited_without_breaking_the_signature(client):
    """The expiry is inside what is signed, not beside it."""
    issued, _expires, kitchen, signature = merchant_auth.issue_cookie().split(":")
    tampered = f"{issued}:{int(time.time()) + 999999}:{kitchen}:{signature}"
    assert merchant_auth.cookie_is_valid(tampered) is False


def test_the_kitchen_cannot_be_edited_without_breaking_the_signature(client):
    """This is the whole security of multi-tenancy: the merchant id in
    this cookie is what every merchant-facing read is scoped by, so a
    session that could be re-pointed at another kitchen would be a
    session that could read somebody else's orders."""
    cookie = merchant_auth.issue_cookie(merchant_id="lahori-grill")
    assert merchant_auth.merchant_from_cookie(cookie) == "lahori-grill"

    tampered = cookie.replace("lahori-grill", "ammas-kitchen")
    assert merchant_auth.merchant_from_cookie(tampered) is None
    assert merchant_auth.cookie_is_valid(tampered) is False


def test_a_cookie_from_before_the_platform_still_signs_in(client):
    """Three parts and no kitchen. Honoured as the default rather than
    rejected: the signature still proves it, and logging every open
    session out to add a field is a worse answer than reading the one it
    was issued under."""
    import merchants

    now = int(time.time())
    payload = f"{now}:{now + 3600}"
    legacy = f"{payload}:{merchant_auth._sign(payload)}"
    assert merchant_auth.merchant_from_cookie(legacy) == merchants.default_id()


def test_a_kitchen_that_is_not_on_the_platform_proves_nothing(client):
    """Signed correctly, but naming a shop that does not exist."""
    now = int(time.time())
    payload = f"{now}:{now + 3600}:no-such-kitchen"
    forged = f"{payload}:{merchant_auth._sign(payload)}"
    assert merchant_auth.merchant_from_cookie(forged) is None


def test_a_cookie_signed_with_another_key_is_rejected(client, monkeypatch):
    valid = merchant_auth.issue_cookie()
    monkeypatch.setenv("SECRET_KEY", "a-completely-different-key")
    assert merchant_auth.cookie_is_valid(valid) is False


def test_the_cookie_is_httponly_and_samesite(client):
    resp = client.post("/merchant/login", data={"password": PASSWORD})
    header = resp.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_logging_out_clears_the_session(merchant):
    assert merchant.get("/api/merchant-config").status_code == 200
    merchant.post("/merchant/logout")
    assert merchant.get("/api/merchant-config").status_code == 401


def test_secrets_are_compared_in_constant_time():
    source = open(merchant_auth.__file__, encoding="utf-8").read()
    assert source.count("compare_digest") >= 2
    for bad in ("supplied == expected", "password == ", "signature == "):
        assert bad not in source


def test_nothing_logs_the_password_or_the_cookie():
    """A credential in a log file is a credential."""
    for module in (merchant_auth, __import__("app")):
        source = open(module.__file__, encoding="utf-8").read()
        for line in source.splitlines():
            if "log." in line or "print(" in line:
                assert "password" not in line.lower(), line.strip()
                assert "cookie" not in line.lower() or "COOKIE_NAME" in line, line.strip()


def test_the_login_does_not_redirect_off_site(client):
    """An open redirect would turn the login into a way to send somebody
    somewhere else."""
    resp = client.post("/merchant/login",
                       data={"password": PASSWORD, "next": "//evil.example/x"})
    assert resp.headers["location"] == "/merchant/orders"


# ------------------------------ the escalation accept/reject endpoints

ACCEPT_REJECT = [
    "/acp/checkout_sessions/abc/human_confirm",
    "/acp/checkout_sessions/abc/human_reject",
    "/ap2/intent-mandates/abc/human-confirm",
    "/ap2/intent-mandates/abc/human-reject",
    "/x402/orders/abc/human_confirm",
    "/x402/orders/abc/human_reject",
    "/mcp-orders/1/human_confirm",
    "/mcp-orders/1/human_reject",
]


@pytest.mark.parametrize("path", ACCEPT_REJECT)
def test_deciding_an_escalation_needs_a_login(client, path):
    """These live in the adapters, which this task must not edit, so they
    are guarded by path in middleware that calls the same
    `is_authenticated` the dependency does."""
    assert client.post(path, json={}).status_code == 401


@pytest.mark.parametrize("path", ACCEPT_REJECT)
def test_a_logged_in_merchant_can_reach_them(merchant, path):
    """404 or 400 is the route being reached with a made-up id, which is
    what proves the guard let it past."""
    assert merchant.post(path, json={}).status_code != 401


def test_the_buyer_facing_adapter_endpoints_are_not_guarded(client):
    """That IS the shopfront. Guarding it would close the shop."""
    resp = client.post("/acp/checkout_sessions",
                       json={"agent_id": "auth-test", "items": [{"item_id": "masala_dosa", "qty": 1}]})
    assert resp.status_code == 200


# ==========================================================================
# The test that stops this regressing
# ==========================================================================

def _declared_paths(app_module):
    """Every route's (methods, path), flattened past FastAPI's included-router
    wrappers -- the adapters are mounted through those, so a naive walk of
    app.routes misses them entirely."""
    out = []

    def walk(routes):
        for r in routes:
            if type(r).__name__ == "_IncludedRouter":
                walk(r.original_router.routes)
                continue
            path = getattr(r, "path", None)
            methods = {m for m in (getattr(r, "methods", None) or set())
                       if m not in ("HEAD", "OPTIONS")}
            if path and methods:
                out.append((r, path, methods))

    walk(app_module.app.routes)
    return out


def _has_guard(route) -> bool:
    return any(
        getattr(d.call, "__name__", "") == "require_merchant"
        for d in getattr(route, "dependant", None).dependencies
    ) if getattr(route, "dependant", None) else False


def test_a_new_merchant_route_cannot_be_added_without_the_guard():
    """THE important one.

    Everything else in this file describes what is true today. This one
    fails the moment somebody adds an `/api/merchant/*` endpoint and
    forgets the dependency -- which is exactly how a login quietly stops
    covering the thing it was added for.

    It is deliberately a set comparison rather than a loop with a skip
    list: a new route either appears in the expected set on purpose, or it
    fails here.
    """
    import app

    routes = _declared_paths(app)

    merchant_api = {path for _r, path, _m in routes if path.startswith("/api/merchant")}
    guarded = {path for r, path, _m in routes if _has_guard(r)}

    unguarded = merchant_api - guarded
    assert not unguarded, (
        f"these /api/merchant/* routes have no require_merchant dependency: "
        f"{sorted(unguarded)}. Add dependencies=[Depends(merchant_auth.require_merchant)]."
    )


def test_the_guarded_set_is_exactly_what_we_intend():
    """The other direction: something becoming guarded by accident is also
    a change worth noticing, because it is how the buyer console or the
    catalog would silently stop working."""
    import app

    expected = {
        "/merchant",
        "/merchant/orders",
        "/api/merchant-config",
        "/api/merchant/optimize-prices",
        "/api/insights",
        "/api/disputes",
        "/api/orders/{order_id}/dispute",
        "/api/evidence/{order_id}",
        "/evidence/{order_id}",
        "/api/sms",
    }
    guarded = {path for r, path, _m in _declared_paths(app) if _has_guard(r)}
    assert guarded == expected, (
        f"guarded set drifted.\n  newly guarded: {sorted(guarded - expected)}"
        f"\n  no longer guarded: {sorted(expected - guarded)}"
    )


def test_the_things_that_must_never_be_guarded_are_not():
    """/mcp behind a cookie breaks the Claude connector; /catalog behind
    one means no agent can read the menu; the webhooks have their own,
    stronger authentication."""
    import app

    for r, path, _m in _declared_paths(app):
        if path.startswith(("/catalog", "/mcp", "/webhook", "/buyer", "/audit", "/acp/checkout_sessions")):
            if path.endswith(merchant_auth.PROTECTED_SUFFIXES):
                continue     # accept/reject, guarded on purpose
            assert not _has_guard(r), f"{path} must not require a merchant login"
        assert not merchant_auth.path_needs_merchant("/mcp")
        assert not merchant_auth.path_needs_merchant("/catalog")


def test_the_audit_trail_is_readable_without_a_login_and_carries_no_pii(client):
    """Deliberate: a trail behind a login is a claim you have to take on
    trust, and being checkable by someone with no account is the whole
    point of it. So the page stays open and the customer's name, phone and
    address are redacted out of it instead -- the full record is at
    /evidence/<id>, which does need the login."""
    import audit_log
    import dashboard

    # Built here rather than fished out of whatever the database happens
    # to hold, so this actually runs instead of skipping on a clean one.
    row = {
        "id": 1, "ts": "2026-08-29T10:00:00+00:00", "agent_id": "agent-pii",
        "protocol": "mcp", "decision": "APPROVE", "reason": "within budget",
        "cart_json": '[{"item": "veg_thali", "qty": 1}]', "total_inr": 150,
        "payment_id": None, "payment_link_id": None, "order_ref": None,
        "buyer_reasoning": None, "limits_snapshot": None, "disputed_at": None,
        "source": None, "routine_id": None,
        "delivery_name": "Jeet Manseta",
        "delivery_phone": "8306610707",
        "delivery_address": "Sharad Appartment, Sardar Nagar West",
    }
    page = dashboard._render([row], audit_log.DEFAULT_DB_PATH, 0)

    for field in ("delivery_name", "delivery_phone", "delivery_address"):
        assert row[field] not in page, f"/audit leaks {field}"
    assert "needs a merchant login" in page, "the redaction should say where the record is"

    assert client.get("/audit").status_code == 200


# --------------------------------------------------- Google sign-in

def test_merchant_google_stays_shut_when_unconfigured(monkeypatch):
    """Blank means closed. An unconfigured door should be a closed one."""
    import google_auth

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.delenv("MERCHANT_GOOGLE_EMAILS", raising=False)

    assert google_auth.merchant_google_enabled() is False
    with pytest.raises(google_auth.NotConfigured):
        google_auth.verify_merchant("anything")


def test_an_allowlist_of_one_lets_only_that_person_in(monkeypatch):
    import google_auth

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setenv("MERCHANT_GOOGLE_EMAILS", "amma@example.com")

    assert google_auth.merchant_is_open_to_anyone() is False
    assert google_auth.merchant_emails() == {"amma@example.com"}


def test_the_wildcard_is_explicit_not_a_default(monkeypatch):
    """`*` opens the shop to any Google account. It exists so that choice
    is something somebody typed, rather than what you get by leaving a
    setting blank."""
    import google_auth

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setenv("MERCHANT_GOOGLE_EMAILS", google_auth.OPEN_TO_ANYONE)

    assert google_auth.merchant_is_open_to_anyone() is True
    assert google_auth.merchant_google_enabled() is True

    # And it is still a REAL Google account -- the signature is verified
    # either way. "Anyone with a Google account" is not "anyone".
    with pytest.raises(google_auth.NotAllowed):
        google_auth.verify_merchant("not.a.real.token")


def test_a_forged_token_is_refused_even_with_the_wildcard(monkeypatch):
    """The wildcard relaxes WHO may sign in, never WHETHER the token is
    checked. An unverified JWT is a string the client wrote."""
    import google_auth

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setenv("MERCHANT_GOOGLE_EMAILS", "*")

    import base64
    import json as _json

    def _b64(obj):
        return base64.urlsafe_b64encode(_json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = ".".join([
        _b64({"alg": "none", "typ": "JWT"}),
        _b64({"email": "attacker@example.com", "email_verified": True,
              "sub": "1", "aud": "test.apps.googleusercontent.com",
              "iss": "accounts.google.com", "exp": 9999999999, "iat": 1}),
        "",
    ])
    with pytest.raises(google_auth.NotAllowed):
        google_auth.verify_merchant(forged)


def test_the_buyer_side_has_no_allowlist(monkeypatch):
    """Any Google account is a legitimate customer; the identity is the
    point rather than a gate."""
    import inspect

    import google_auth

    source = inspect.getsource(google_auth.verify)
    assert "merchant_emails" not in source
    assert "allowlist" not in source.lower()
