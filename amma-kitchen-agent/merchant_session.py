"""Log a script in the way a person logs in.

`demo.py`, the human_confirm/human_reject CLIs and the pre-demo check all
drive merchant surfaces that now need a session. They get one by POSTing
the password from the environment to the same `/merchant/login` a browser
uses -- not by a bypass flag.

That distinction is the whole point. A `--skip-auth` switch, or a header
that means "trust me", is a second way in, and a second way in is the one
an attacker reads the repository to find. There is exactly one door here
and these scripts walk through it.
"""

import os

import requests


class NotConfigured(RuntimeError):
    pass


def login(base_url: str, session: requests.Session | None = None) -> requests.Session:
    """Return a session holding a merchant cookie.

    Raises rather than continuing unauthenticated, because a script that
    quietly proceeds without a session fails later at something that
    looks unrelated -- a 401 on an accept, three steps into a demo.
    """
    password = os.environ.get("MERCHANT_CONSOLE_PASSWORD")
    if not password:
        raise NotConfigured(
            "MERCHANT_CONSOLE_PASSWORD is not set. The merchant console and the "
            "accept/reject endpoints need it; add it to .env (see .env.example)."
        )

    session = session or requests.Session()
    resp = session.post(
        f"{base_url.rstrip('/')}/merchant/login",
        data={"password": password, "next": "/merchant/orders"},
        allow_redirects=False,
        timeout=20,
    )
    # A wrong password re-renders the login page as 200 rather than
    # erroring, so "did we get a cookie" is the thing to check.
    import merchant_auth

    if merchant_auth.COOKIE_NAME not in session.cookies:
        raise NotConfigured(
            f"Logging in to {base_url} failed (HTTP {resp.status_code}). Check "
            "MERCHANT_CONSOLE_PASSWORD matches the running server's."
        )
    return session
