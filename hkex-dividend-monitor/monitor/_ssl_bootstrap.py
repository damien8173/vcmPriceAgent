"""Make outbound HTTPS trust the operating system's certificate store.

Python's `requests`/`urllib3` verify TLS against the bundled `certifi` CA
list, NOT the OS trust store. On a network that does TLS inspection -- a
corporate proxy, VPN, or antivirus that re-signs HTTPS with its own root CA
(Zscaler, Palo Alto, Kaspersky, etc.) -- every outbound call the app makes
(Slack/Discord/Telegram webhooks, HKEX search, DeepSeek, document downloads)
then fails with:

    SSLCertVerificationError: self-signed certificate in certificate chain

even though the machine itself already trusts that root CA (its browser works
fine). `truststore` bridges the gap: it points Python's ssl module at the
OS-native store -- Windows CryptoAPI, macOS Security framework, or Linux
/etc/ssl/certs -- which already contains whatever CA your IT department
deployed. So the fix needs zero certificate wrangling from the user on a
normally-managed machine; in Docker the same store is populated at build time
from ./certs (see certs/README.md and the Dockerfile).

Called once per process at import time (see monitor/__init__.py), before any
`requests` call is made. Best-effort by design: if truststore isn't installed
or injection fails for any reason, we record why and fall back to certifi
rather than let TLS setup crash startup. Set MONITOR_DISABLE_TRUSTSTORE=1 to
opt out and force the plain certifi behaviour.
"""
from __future__ import annotations

import os

# Process-wide, set on first call. Also serves as an idempotency guard so a
# second import doesn't inject twice. Exposed for get_status()/tests.
_STATUS: str | None = None

_TRUTHY = {"1", "true", "yes", "on"}


def enable_system_trust_store() -> str:
    """Route Python's TLS verification through the OS trust store.

    Idempotent: injection happens at most once per process. Returns a short
    status string -- "injected", "disabled" (opted out), "unavailable"
    (truststore not installed), or "failed: <reason>" -- for logging and the
    dashboard's system-status panel.
    """
    global _STATUS
    if _STATUS is not None:
        return _STATUS

    if os.environ.get("MONITOR_DISABLE_TRUSTSTORE", "").strip().lower() in _TRUTHY:
        _STATUS = "disabled"
        return _STATUS

    try:
        import truststore

        truststore.inject_into_ssl()
        _STATUS = "injected"
    except ImportError:
        # truststore is an ordinary dependency (requirements.txt), but keep
        # working if someone runs against a partial/older install.
        _STATUS = "unavailable"
    except Exception as exc:  # noqa: BLE001 - never let TLS setup crash startup
        _STATUS = f"failed: {exc}"

    return _STATUS


def trust_store_status() -> str:
    """The status recorded by enable_system_trust_store(), or "not-run" if it
    somehow hasn't executed yet."""
    return _STATUS or "not-run"
