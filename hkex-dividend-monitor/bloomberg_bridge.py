"""Standalone Bloomberg Desktop API (DAPI) bridge.

Bloomberg's Desktop API only accepts connections from a process running on
the *exact same physical machine* as a logged-in Bloomberg Terminal --
it listens on 127.0.0.1 and is not reachable from inside a Docker container
or from any other machine on the network. The HKEX Dividend Monitor itself
runs in Docker, so it cannot import ``blpapi`` directly.

This script is the workaround: run it directly on the Bloomberg Terminal PC
(outside Docker, with no dependency on the rest of this repo). It talks to
the Terminal as a normal localhost ``blpapi`` client, then re-serves the
data it gets back over a tiny HTTP API that the Dockerized app can reach
across the network.

Requirements: Python 3 (standard library only) plus Bloomberg's ``blpapi``
package, which is not on PyPI and must be installed from Bloomberg's own
package index:

    pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple blpapi

This machine must have the Bloomberg Terminal application open and logged
in for ``blpapi`` to be able to connect.

Environment variables (all optional):
    BLOOMBERG_BRIDGE_PORT   Port this bridge listens on. Default: 8195.
    BLOOMBERG_TERMINAL_HOST Host where the local Bloomberg Terminal/BBComm
                            listens. Default: 127.0.0.1.
    BLOOMBERG_TERMINAL_PORT Port where the local Bloomberg Terminal/BBComm
                            listens. Default: 8194.
    BLOOMBERG_TOKEN         Optional shared secret. When set, callers must
                            send it in an X-Bloomberg-Token header on every
                            request. Default: unset (no auth required).
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_PIP_INSTALL_HINT = (
    "pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple blpapi"
)

try:
    import blpapi
except ImportError:
    print(
        "ERROR: the 'blpapi' package is not installed.\n"
        "This bridge only works on a machine with the Bloomberg Terminal\n"
        "installed and it needs Bloomberg's own Python package, which is not\n"
        "on PyPI. Install it with:\n\n"
        f"    {_PIP_INSTALL_HINT}\n",
        file=sys.stderr,
    )
    sys.exit(1)


BLOOMBERG_BRIDGE_PORT = int(os.environ.get("BLOOMBERG_BRIDGE_PORT", "8195"))
BLOOMBERG_TERMINAL_HOST = os.environ.get("BLOOMBERG_TERMINAL_HOST", "127.0.0.1")
BLOOMBERG_TERMINAL_PORT = int(os.environ.get("BLOOMBERG_TERMINAL_PORT", "8194"))
BLOOMBERG_TOKEN = os.environ.get("BLOOMBERG_TOKEN", "")

# Must match monitor/bloomberg.py's DIVIDEND_FIELDS exactly. Used only as a
# fallback default when a request doesn't specify its own fields list.
DEFAULT_FIELDS = (
    "DVD_SH_LAST",
    "BDVD_NEXT_PROJECTED_DVD",
    "DVD_EX_DT",
    "DVD_DECLARED_DT",
    "BDVD_NEXT_EST_EX_DT",
)

_REFDATA_SERVICE = "//blp/refdata"
_REQUEST_TIMEOUT_SECONDS = 15


def fetch_reference_data(securities: list[str], fields: list[str] | None = None) -> list[dict[str, Any]]:
    """Run one Bloomberg ReferenceDataRequest for `securities` and `fields`.

    Returns a list of dicts, one per requested security and in the same
    order they were requested:
        {"ticker": <security name as Bloomberg echoed it back>,
         "fields": {<field mnemonic>: <value>, ...},
         "errors": [<per-security error/warning strings>]}

    A single bad security (e.g. an invalid ticker) never aborts the whole
    batch -- only that security's row carries an error; every other row in
    the same request still returns its data.
    """
    if not securities:
        return []
    field_list = list(fields) if fields else list(DEFAULT_FIELDS)

    session_options = blpapi.SessionOptions()
    session_options.setServerHost(BLOOMBERG_TERMINAL_HOST)
    session_options.setServerPort(BLOOMBERG_TERMINAL_PORT)
    session = blpapi.Session(session_options)

    try:
        if not session.start():
            raise RuntimeError(
                "Could not start a Bloomberg session -- check that the "
                "Bloomberg Terminal is open and logged in on this machine."
            )
        if not session.openService(_REFDATA_SERVICE):
            raise RuntimeError(
                f"Could not open the Bloomberg {_REFDATA_SERVICE} service -- "
                "check that the Bloomberg Terminal is open and logged in on "
                "this machine."
            )

        service = session.getService(_REFDATA_SERVICE)
        request = service.createRequest("ReferenceDataRequest")

        securities_element = request.getElement("securities")
        for security in securities:
            securities_element.appendValue(security)

        fields_element = request.getElement("fields")
        for field in field_list:
            fields_element.appendValue(field)

        session.sendRequest(request)

        # Bloomberg may split the response across several messages/events,
        # and doesn't strictly guarantee they arrive in request order, so
        # match returned rows back to request slots by security name (using
        # a per-name queue to handle any duplicate names in the request).
        pending_slots: dict[str, deque[int]] = {}
        for idx, name in enumerate(securities):
            pending_slots.setdefault(name, deque()).append(idx)

        results: list[dict[str, Any] | None] = [None] * len(securities)
        deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {_REQUEST_TIMEOUT_SECONDS}s waiting for a "
                    "response from the Bloomberg Terminal."
                )
            event = session.nextEvent(int(max(remaining, 0.05) * 1000))

            for message in event:
                _consume_reference_data_message(message, field_list, pending_slots, results)

            if event.eventType() == blpapi.Event.RESPONSE:
                break

        # Defensive fallback: any slot Bloomberg never mentioned back still
        # gets a row, so the response shape always matches the request.
        for idx, name in enumerate(securities):
            if results[idx] is None:
                results[idx] = {
                    "ticker": name,
                    "fields": {},
                    "errors": ["No data returned by Bloomberg for this security."],
                }

        return results  # type: ignore[return-value]
    finally:
        session.stop()


def _consume_reference_data_message(
    message: Any,
    requested_fields: list[str],
    pending_slots: dict[str, "deque[int]"],
    results: list[dict[str, Any] | None],
) -> None:
    if not message.hasElement("securityData"):
        return
    security_data_array = message.getElement("securityData")
    for i in range(security_data_array.numValues()):
        security_data = security_data_array.getValueAsElement(i)
        name = security_data.getElementAsString("security")
        row = _parse_security_data(security_data, requested_fields, name)

        slots = pending_slots.get(name)
        if slots:
            results[slots.popleft()] = row
        else:
            # Unexpected/extra entry -- append into the first free slot
            # rather than silently dropping the data.
            for idx, existing in enumerate(results):
                if existing is None:
                    results[idx] = row
                    break


def _parse_security_data(security_data: Any, requested_fields: list[str], name: str) -> dict[str, Any]:
    errors: list[str] = []
    field_values: dict[str, Any] = {}

    if security_data.hasElement("securityError"):
        errors.append(_format_error_element(security_data.getElement("securityError")))
        return {"ticker": name, "fields": field_values, "errors": errors}

    if security_data.hasElement("fieldData"):
        field_data = security_data.getElement("fieldData")
        for field in requested_fields:
            if field_data.hasElement(field):
                field_values[field] = _extract_value(field_data.getElement(field))

    if security_data.hasElement("fieldExceptions"):
        field_exceptions = security_data.getElement("fieldExceptions")
        for i in range(field_exceptions.numValues()):
            exception_element = field_exceptions.getValueAsElement(i)
            try:
                field_id = (
                    exception_element.getElementAsString("fieldId")
                    if exception_element.hasElement("fieldId")
                    else "?"
                )
                if exception_element.hasElement("errorInfo"):
                    errors.append(f"{field_id}: {_format_error_element(exception_element.getElement('errorInfo'))}")
            except Exception as exc:  # noqa: BLE001 - never let a weird field crash the batch
                errors.append(f"Could not read field exception: {exc}")

    return {"ticker": name, "fields": field_values, "errors": errors}


def _format_error_element(error_element: Any) -> str:
    try:
        category = error_element.getElementAsString("category") if error_element.hasElement("category") else ""
        message = (
            error_element.getElementAsString("message")
            if error_element.hasElement("message")
            else str(error_element)
        )
        return f"{category}: {message}" if category else message
    except Exception:  # noqa: BLE001 - fall back to something rather than crash
        return str(error_element)


def _extract_value(element: Any) -> Any:
    try:
        return element.getValueAsString()
    except Exception:  # noqa: BLE001 - unusual Bloomberg value type
        try:
            return element.getValue()
        except Exception:  # noqa: BLE001 - never let one odd value kill the request
            return None


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "BloombergBridge/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - matches base signature
        print(f"[bloomberg_bridge] {self.address_string()} - {format % args}")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001 - client disconnected mid-response, nothing to do
            pass

    def _check_token(self) -> bool:
        if not BLOOMBERG_TOKEN:
            return True
        return self.headers.get("X-Bloomberg-Token", "") == BLOOMBERG_TOKEN

    def do_GET(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler name
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": f"Unknown endpoint: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler name
        if self.path != "/dividends":
            self._send_json(404, {"error": f"Unknown endpoint: {self.path}"})
            return

        if not self._check_token():
            self._send_json(401, {"error": "Missing or invalid X-Bloomberg-Token header."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            self._send_json(400, {"error": "Request body must be valid JSON."})
            return

        securities = payload.get("securities") if isinstance(payload, dict) else None
        if not isinstance(securities, list) or not securities:
            self._send_json(400, {"error": "'securities' must be a non-empty list of Bloomberg security names."})
            return

        fields = payload.get("fields") if isinstance(payload, dict) else None

        try:
            rows = fetch_reference_data(securities, fields)
        except Exception as exc:  # noqa: BLE001 - report connectivity/session errors, never crash
            self._send_json(503, {"error": f"Bloomberg request failed: {exc}"})
            return

        self._send_json(200, {"securities": rows})


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", BLOOMBERG_BRIDGE_PORT), _BridgeRequestHandler)  # noqa: S104 - must be reachable from other machines/containers

    print("============================================")
    print("  Bloomberg Bridge - Starting")
    print("============================================")
    print(f"Listening on: 0.0.0.0:{BLOOMBERG_BRIDGE_PORT}")
    print(f"Forwarding to Bloomberg Terminal at: {BLOOMBERG_TERMINAL_HOST}:{BLOOMBERG_TERMINAL_PORT}")
    print(f"Token auth: {'ENABLED' if BLOOMBERG_TOKEN else 'disabled (set BLOOMBERG_TOKEN to enable)'}")
    print()
    print("Keep this window open while you want Bloomberg data available to")
    print("the HKEX Dividend Monitor. Press Ctrl+C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Bloomberg bridge...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
