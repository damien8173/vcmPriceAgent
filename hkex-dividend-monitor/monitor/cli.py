"""Command-line interface for the HKEX dividend monitor.

Usage (inside the container or a local venv):
    python -m monitor.cli add-target 700 2026-08-15
    python -m monitor.cli remove-target 700
    python -m monitor.cli list
    python -m monitor.cli status
    python -m monitor.cli test-alert
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from monitor.config import HEARTBEAT_FILE, get_config
from monitor.daemon import racing_targets
from monitor.db import health as db_health
from monitor.notifier import AlertPayload, configured_channels, dispatch_alert
from monitor.registry import NotifiedCache, TargetRegistry, normalize_ticker, validate_date

registry = TargetRegistry()
notified_cache = NotifiedCache()


def cmd_add_target(args: argparse.Namespace) -> int:
    try:
        entry = registry.add_target(args.ticker, args.target_date)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Added/updated target: {entry}")
    return 0


def cmd_remove_target(args: argparse.Namespace) -> int:
    try:
        ticker = normalize_ticker(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    removed = registry.remove_target(ticker)
    if removed:
        print(f"Removed {removed} target(s) for ticker {ticker}")
        return 0
    print(f"No targets found for ticker {ticker}")
    return 1


def cmd_deactivate(args: argparse.Namespace) -> int:
    try:
        ticker = normalize_ticker(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    changed = registry.set_status(ticker, "inactive")
    print(f"Deactivated {changed} target(s) for ticker {ticker}" if changed else f"No targets found for {ticker}")
    return 0 if changed else 1


def cmd_list(_args: argparse.Namespace) -> int:
    targets = registry.load()
    if not targets:
        print("No targets registered.")
        return 0
    print(f"{'TICKER':<8}{'TARGET DATE':<14}{'STATUS':<10}")
    for t in targets:
        print(f"{t['ticker']:<8}{t['target_date']:<14}{t['status']:<10}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = get_config()
    print("=== HKEX Dividend Monitor Status ===\n")

    # SurrealDB
    db_ok = db_health()
    print(f"SurrealDB ({cfg.surreal_endpoint}): {'HEALTHY' if db_ok else 'UNREACHABLE'}")

    # Daemon heartbeat
    if HEARTBEAT_FILE.exists():
        raw = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
        try:
            hb_time = datetime.fromisoformat(raw)
            age = (datetime.now(hb_time.tzinfo) - hb_time).total_seconds()
            healthy = age < (2 * cfg.poll_interval_seconds)
            status_str = "HEALTHY" if healthy else "STALE"
            print(f"Daemon heartbeat: {status_str} (last seen {int(age)}s ago, at {raw})")
        except ValueError:
            print(f"Daemon heartbeat: UNKNOWN (unparseable: {raw!r})")
    else:
        print("Daemon heartbeat: NOT FOUND (daemon may not have started yet)")

    # Notification channels
    channels = configured_channels()
    print(f"Configured notification channels: {', '.join(channels) if channels else 'NONE'}")

    # LLM
    llm_ok = bool(cfg.deepseek_api_key)
    print(f"DeepSeek API key configured: {'YES' if llm_ok else 'NO'}")

    # Targets
    targets = registry.load()
    active = [t for t in targets if t["status"] == "active"]
    print(f"\nTargets: {len(targets)} total, {len(active)} active")
    for t in active:
        print(f"  - {t['ticker']} -> {t['target_date']}")

    # Race mode
    racing = sorted({t["ticker"] for t in racing_targets(active)})
    print(
        f"\nRace mode (target date = today): "
        f"{', '.join(racing) if racing else 'not active'} "
        f"(poll every {cfg.race_poll_interval_seconds}s, "
        f"window {cfg.race_start_hour:02d}:00-{cfg.race_end_hour:02d}:00 HKT)"
    )

    # Notified cache stats
    nc = notified_cache.load()
    print(
        f"\nAlerts sent: {len(nc['notified'])} | "
        f"Processed (no alert / gave up): {len(nc['processed'])} | "
        f"Pending retries: {len(nc['failed'])}"
    )
    return 0


def cmd_test_alert(_args: argparse.Namespace) -> int:
    channels = configured_channels()
    if not channels:
        print("No notification channels configured. Set one in the web dashboard's "
              "Settings tab, or via SLACK_WEBHOOK_URL / DISCORD_WEBHOOK_URL / "
              "TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID in .env.")
        return 1

    payload = AlertPayload(
        ticker="00700",
        company_name="Example Holdings Limited (TEST)",
        payout_amount="HKD 0.45 per share",
        ex_dividend_date="2026-08-01",
        payment_date="2026-08-15",
        source_url="https://example.com/test-filing.pdf",
    )
    results = dispatch_alert(payload)
    for channel, ok in results.items():
        if channel in channels:
            print(f"{channel}: {'SENT' if ok else 'FAILED (see diagnostics.log)'}")
    return 0 if any(results[c] for c in channels) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor", description="HKEX Dividend Monitor CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-target", help="Add or reactivate a ticker/date watch target")
    p_add.add_argument("ticker", help="HKEX stock code, e.g. 700 or 00700")
    p_add.add_argument("target_date", help="Target filing date, YYYY-MM-DD")
    p_add.set_defaults(func=cmd_add_target)

    p_remove = sub.add_parser("remove-target", help="Remove a ticker from the watchlist entirely")
    p_remove.add_argument("ticker", help="HKEX stock code")
    p_remove.set_defaults(func=cmd_remove_target)

    p_deact = sub.add_parser("deactivate", help="Mark a ticker's target(s) inactive without deleting")
    p_deact.add_argument("ticker", help="HKEX stock code")
    p_deact.set_defaults(func=cmd_deactivate)

    p_list = sub.add_parser("list", help="List all watch targets")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="Show daemon/DB/notification health and target summary")
    p_status.set_defaults(func=cmd_status)

    p_test = sub.add_parser("test-alert", help="Send a test alert to all configured channels")
    p_test.set_defaults(func=cmd_test_alert)

    return parser


def main() -> int:
    get_config().ensure_data_dir()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
