"""HKEX Dividend Monitor.

Importing this package (once per daemon / web / CLI process) points Python's
TLS verification at the operating system's certificate store, so the app works
on networks that do HTTPS inspection with a corporate root CA. Done here, at
the single import chokepoint every entry point passes through, so it takes
effect before any outbound `requests` call -- see monitor._ssl_bootstrap.
"""
from monitor._ssl_bootstrap import enable_system_trust_store

enable_system_trust_store()
