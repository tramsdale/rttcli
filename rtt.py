#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
#     "rich",
# ]
# ///

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

CONFIG_PATH = Path.home() / ".config" / "rtt" / "config.json"
BASE_URL = "https://data.rtt.io"

_term_width = shutil.get_terminal_size(fallback=(120, 24)).columns
console = Console(width=max(_term_width, 100))


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def save_token(token: str) -> None:
    cfg = load_config()
    cfg["refresh_token"] = token
    # Clear any cached access token when a new refresh token is set
    cfg.pop("access_token", None)
    cfg.pop("access_token_valid_until", None)
    save_config(cfg)
    console.print(f"[green]Token saved to {CONFIG_PATH}[/green]")


def get_access_token() -> str:
    """
    Return a valid access token, exchanging the refresh token if needed.
    Access tokens are cached in config until 30s before expiry.
    """
    cfg = load_config()
    refresh = cfg.get("refresh_token", "")
    if not refresh:
        console.print(f"[red]No token configured.[/red] Run: [bold]rtt config --token YOUR_TOKEN[/bold]")
        sys.exit(1)

    # Check cached access token
    cached = cfg.get("access_token", "")
    valid_until_str = cfg.get("access_token_valid_until", "")
    if cached and valid_until_str:
        try:
            valid_until = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
            now = datetime.now(tz=valid_until.tzinfo)
            if now < valid_until - timedelta(seconds=30):
                return cached
        except ValueError:
            pass

    # Exchange refresh token for access token
    r = requests.get(
        f"{BASE_URL}/api/get_access_token",
        headers={"Authorization": f"Bearer {refresh}"},
        timeout=10,
    )
    if r.status_code == 401:
        console.print("[red]Refresh token rejected. Re-run:[/red] rtt config --token YOUR_TOKEN")
        sys.exit(1)
    if not r.ok:
        console.print(f"[red]Token exchange failed {r.status_code}:[/red] {r.text[:200]}")
        sys.exit(1)

    data = r.json()
    access = data["token"]
    valid_until = data.get("validUntil", "")

    cfg["access_token"] = access
    cfg["access_token_valid_until"] = valid_until
    save_config(cfg)

    return access


# ── Date/time helpers ─────────────────────────────────────────────────────────

def next_weekday(target: int) -> date:
    """Next occurrence of weekday (0=Mon … 6=Sun), always strictly in the future."""
    today = date.today()
    days = (target - today.weekday()) % 7
    if days == 0:
        days = 7
    return today + timedelta(days=days)


def parse_date(s: str) -> date:
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    console.print(f"[red]Invalid date '{s}'. Use DD/MM/YY, e.g. 7/6/26[/red]")
    sys.exit(1)


def parse_hhmm(s: str) -> tuple[int, int]:
    s = s.zfill(4)
    if len(s) == 4 and s.isdigit():
        h, m = int(s[:2]), int(s[2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    console.print(f"[red]Invalid time '{s}'. Use HHMM, e.g. 2100[/red]")
    sys.exit(1)


def fmt_iso(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    return dt_str[11:16]  # "2026-06-07T21:30:00Z" → "21:30"


# ── API calls ─────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


def api_search(from_crs: str, to_crs: str, time_from: str) -> dict | None:
    params = {
        "code": from_crs.upper(),
        "filterTo": to_crs.upper(),
        "timeFrom": time_from,
        "timeWindow": 240,  # show next 4 hours of departures
    }
    r = requests.get(f"{BASE_URL}/gb-nr/location", headers=_headers(), params=params, timeout=10)
    if r.status_code == 204:
        return None
    if r.status_code == 401:
        console.print("[red]Unauthorised — re-run:[/red] rtt config --token YOUR_TOKEN")
        sys.exit(1)
    if not r.ok:
        console.print(f"[red]API error {r.status_code}:[/red] {r.text[:200]}")
        sys.exit(1)
    return r.json()


def api_service(identity: str, dep_date: str) -> dict:
    params = {"identity": identity, "departureDate": dep_date}
    r = requests.get(f"{BASE_URL}/gb-nr/service", headers=_headers(), params=params, timeout=10)
    if not r.ok:
        console.print(f"[red]API error {r.status_code}:[/red] {r.text[:200]}")
        sys.exit(1)
    return r.json()


# ── Time display logic ────────────────────────────────────────────────────────

def _time_status(timing: dict) -> tuple[str, str]:
    """
    Returns (display_str, status) where status is one of:
    'cancelled', 'delayed', 'on_time', 'forecast', 'scheduled'
    """
    if not timing:
        return "-", "scheduled"

    sched = fmt_iso(timing.get("scheduleAdvertised") or timing.get("scheduleInternal"))
    actual = fmt_iso(timing.get("realtimeActual"))
    forecast = fmt_iso(timing.get("realtimeForecast"))
    lateness = timing.get("realtimeAdvertisedLateness") or timing.get("realtimeInternalLateness") or 0
    cancelled = timing.get("isCancelled", False)

    if cancelled:
        return sched or "-", "cancelled"
    if actual:
        if lateness and lateness > 1:
            return f"{sched}→{actual} (+{lateness}m)", "delayed"
        return actual, "on_time"
    if forecast:
        if forecast != sched and lateness and lateness > 1:
            return f"{sched}→{forecast} (+{lateness}m)", "delayed"
        return sched or "-", "scheduled"
    return sched or "-", "scheduled"


def _rich_time(timing: dict, role: str = "departure") -> Text:
    """Render a time cell as Rich Text with appropriate colour."""
    if not timing:
        return Text("-", style="dim")
    display, status = _time_status(timing)
    styles = {
        "cancelled": "strike red",
        "delayed": "yellow",
        "on_time": "green",
        "forecast": "cyan",
        "scheduled": "",
    }
    return Text(display, style=styles.get(status, ""))


def _status_badge(display_as: str, temporal: dict, reasons: list) -> Text:
    dep = temporal.get("departure") or {}
    arr = temporal.get("arrival") or {}
    lateness = (
        dep.get("realtimeAdvertisedLateness")
        or dep.get("realtimeInternalLateness")
        or arr.get("realtimeAdvertisedLateness")
        or arr.get("realtimeInternalLateness")
        or 0
    )
    reason_str = reasons[0].get("shortText", "") if reasons else ""

    if display_as == "CANCELLED":
        return Text("Cancelled", style="bold red")
    if display_as == "DIVERTED":
        return Text("Diverted", style="bold magenta")
    if display_as == "STARTS":
        return Text("Starts here", style="bold cyan")
    if display_as == "TERMINATES":
        return Text("Terminates", style="bold cyan")
    if lateness and lateness > 1:
        label = f"Delayed +{lateness}m"
        if reason_str:
            label += f": {reason_str[:20]}"
        return Text(label, style="bold yellow")
    if dep.get("realtimeActual") or arr.get("realtimeActual"):
        return Text("On time", style="green")
    return Text("Scheduled", style="dim")


# ── Display: departures board ─────────────────────────────────────────────────

def display_departures(data: dict, from_crs: str, to_crs: str) -> list:
    services = data.get("services") or []
    query = data.get("query", {})
    loc_name = query.get("location", {}).get("description", from_crs.upper())
    time_from = fmt_iso(query.get("timeFrom", ""))

    console.print()
    title = f"[bold]Trains from [cyan]{loc_name}[/cyan] → [cyan]{to_crs.upper()}[/cyan][/bold]"
    if time_from:
        title += f"  [dim]from {time_from}[/dim]"
    console.print(title)
    console.print()

    if not services:
        console.print("[yellow]No services found.[/yellow]")
        return []

    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold blue", padding=(0, 1), expand=False)
    tbl.add_column("#", style="dim", width=3, min_width=3, justify="right", no_wrap=True)
    tbl.add_column("Departs", width=8, min_width=5, no_wrap=True)
    tbl.add_column("Code", width=6, min_width=4, no_wrap=True)
    tbl.add_column("Destination", width=24, min_width=16)
    tbl.add_column("Operator", width=20, min_width=12)
    tbl.add_column("Plat", width=4, min_width=4, no_wrap=True)
    tbl.add_column("Status", width=26, min_width=10)

    for i, svc in enumerate(services, 1):
        temporal = svc.get("temporalData", {})
        display_as = temporal.get("displayAs", "CALL") or "CALL"
        sched_meta = svc.get("scheduleMetadata", {})
        loc_meta = svc.get("locationMetadata", {})
        reasons = svc.get("reasons") or []

        headcode = sched_meta.get("trainReportingIdentity", "-")
        operator = sched_meta.get("operator", {}).get("name", "-") or "-"
        if len(operator) > 20:
            operator = operator[:19] + "…"

        destinations = svc.get("destination") or []
        dest = destinations[0].get("location", {}).get("description", "-") if destinations else "-"

        platform_obj = loc_meta.get("platform") or {}
        platform = platform_obj.get("actual") or platform_obj.get("planned") or "-"

        dep_timing = temporal.get("departure") or {}
        dep_display, dep_status = _time_status(dep_timing)
        dep_styles = {
            "cancelled": "strike red",
            "delayed": "yellow",
            "on_time": "green",
            "forecast": "cyan",
            "scheduled": "",
        }
        dep_text = Text(dep_display, style=dep_styles.get(dep_status, ""))

        badge = _status_badge(display_as, temporal, reasons)

        tbl.add_row(str(i), dep_text, headcode, dest, operator, platform, badge)

    console.print(tbl)
    console.print(f"[dim]  Use --detail N to show full calling points for train #N[/dim]")
    return services


# ── Display: service detail ───────────────────────────────────────────────────

def display_service_detail(data: dict, highlight_crs: str | None = None) -> None:
    # Top-level response wraps everything inside a 'service' key
    svc_data = data.get("service", data)
    sched_meta = svc_data.get("scheduleMetadata", {})
    locations = svc_data.get("locations") or []
    system_status = data.get("systemStatus", {})

    identity = sched_meta.get("identity", "?")
    headcode = sched_meta.get("trainReportingIdentity", "-")
    operator = sched_meta.get("operator", {}).get("name", "-") or "-"
    dep_date = sched_meta.get("departureDate", "-")
    mode = sched_meta.get("modeType", "TRAIN")
    stp = sched_meta.get("stpIndicator", "")

    # Use top-level origin/destination arrays if available, else fall back to locations
    origins = svc_data.get("origin") or []
    dests = svc_data.get("destination") or []
    origin_name = origins[0].get("location", {}).get("description", "-") if origins else (
        locations[0].get("location", {}).get("description", "-") if locations else "-"
    )
    dest_name = dests[0].get("location", {}).get("description", "-") if dests else (
        locations[-1].get("location", {}).get("description", "-") if locations else "-"
    )

    # RTT system status banner if degraded
    rtt_core = system_status.get("rttCore", "OK")
    if rtt_core != "OK":
        console.print(f"[bold yellow]⚠ RTT system status: {rtt_core}[/bold yellow]")

    header_lines = [
        f"[bold cyan]{headcode}[/bold cyan]  [bold]{identity}[/bold]  [dim]{stp}[/dim]",
        f"[bold]{origin_name}[/bold] → [bold]{dest_name}[/bold]",
        f"[dim]{operator} · {dep_date} · {mode}[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(header_lines), box=box.ROUNDED, expand=False))
    console.print()

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold blue", padding=(0, 1))
    tbl.add_column("Location", width=28)
    tbl.add_column("Arr", width=14)
    tbl.add_column("Dep", width=14)
    tbl.add_column("Plat", width=5)
    tbl.add_column("Status", width=26)
    tbl.add_column("Notes", width=35)

    for loc in locations:
        temporal = loc.get("temporalData") or {}
        display_as = temporal.get("displayAs")
        location = loc.get("location", {})
        loc_meta = loc.get("locationMetadata") or {}
        reasons = loc.get("reasons") or []
        assocs = loc.get("associatedServices") or []

        # Skip pure passes (no passenger stop)
        if display_as == "PASS" or display_as is None:
            continue

        name = location.get("description", "-")
        short_codes = location.get("shortCodes") or []
        is_highlight = highlight_crs and any(
            c.upper() == highlight_crs.upper() for c in short_codes
        )

        platform_obj = loc_meta.get("platform") or {}
        plat_plan = platform_obj.get("planned") or "-"
        # service detail uses 'forecast'; departures board uses 'actual'
        plat_realtime = platform_obj.get("actual") or platform_obj.get("forecast")
        if plat_realtime and plat_realtime != plat_plan:
            platform = f"{plat_plan}→{plat_realtime}"
            plat_style = "yellow"
        else:
            platform = plat_plan
            plat_style = ""

        arr_timing = temporal.get("arrival") or {}
        dep_timing = temporal.get("departure") or {}

        arr_text = _rich_time(arr_timing)
        dep_text = _rich_time(dep_timing)

        badge = _status_badge(display_as, temporal, reasons)

        # Notes: reasons + associations
        notes_parts = []
        for r in reasons[:2]:
            short = r.get("shortText", "")
            long_ = r.get("longText") or short
            if long_ and long_ != short:
                notes_parts.append(f"{short} ({long_[:30]})")
            elif short:
                notes_parts.append(short)
        for a in assocs[:2]:
            atype = a.get("type", "")
            aloc = (a.get("location") or {}).get("description", "")
            if atype and aloc:
                notes_parts.append(f"{atype}: {aloc}")
        notes = "; ".join(notes_parts)

        # Name style
        if display_as == "CANCELLED":
            name_text = Text(name, style="strike red")
        elif display_as == "DIVERTED":
            name_text = Text(name, style="magenta")
        elif display_as in ("STARTS", "TERMINATES"):
            name_text = Text(name, style="bold")
        elif is_highlight:
            name_text = Text(f"▶ {name}", style="bold cyan")
        else:
            name_text = Text(name)

        tbl.add_row(
            name_text,
            arr_text,
            dep_text,
            Text(platform, style=plat_style),
            badge,
            Text(notes, style="dim"),
        )

    console.print(tbl)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    # Handle `rtt config --token TOKEN` before argparse sees positional args
    if len(sys.argv) >= 2 and sys.argv[1] == "config":
        cfg = argparse.ArgumentParser(prog="rtt config")
        cfg.add_argument("--token", required=True, metavar="TOKEN", help="RTT API Bearer token")
        a = cfg.parse_args(sys.argv[2:])
        save_token(a.token)
        return

    parser = argparse.ArgumentParser(
        prog="rtt",
        description="Real Time Trains CLI  —  rtt FROM TO [options]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  rtt config --token eyJ...          save your API token
  rtt PAD BRI                        next trains Paddington → Bristol
  rtt PAD BRI --after 2100           trains after 21:00
  rtt PAD BRI --tomorrow             tomorrow's trains
  rtt PAD BRI --friday               next Friday's trains
  rtt PAD BRI --date 7/6/26          specific date
  rtt PAD BRI --detail 1             full calling points for first train
  rtt PAD BRI --after 1800 --detail 2
""",
    )

    parser.add_argument("from_station", nargs="?", help="Origin CRS code (e.g. PAD)")
    parser.add_argument("to_station", nargs="?", help="Destination CRS code (e.g. BRI)")

    parser.add_argument("--after", metavar="HHMM", help="Show trains departing after this time")

    day_grp = parser.add_mutually_exclusive_group()
    day_grp.add_argument("--tomorrow", action="store_true")
    day_grp.add_argument("--monday", action="store_true")
    day_grp.add_argument("--tuesday", action="store_true")
    day_grp.add_argument("--wednesday", action="store_true")
    day_grp.add_argument("--thursday", action="store_true")
    day_grp.add_argument("--friday", action="store_true")
    day_grp.add_argument("--saturday", action="store_true")
    day_grp.add_argument("--sunday", action="store_true")
    day_grp.add_argument("--date", metavar="DD/MM/YY", help="Specific date")

    parser.add_argument("--detail", metavar="N", type=int, help="Show full calling points for train #N")

    args = parser.parse_args()

    if not args.from_station or not args.to_station:
        parser.print_help()
        sys.exit(1)

    # ── Resolve target date ──
    today = date.today()
    if args.tomorrow:
        target_date = today + timedelta(days=1)
    elif args.monday:
        target_date = next_weekday(0)
    elif args.tuesday:
        target_date = next_weekday(1)
    elif args.wednesday:
        target_date = next_weekday(2)
    elif args.thursday:
        target_date = next_weekday(3)
    elif args.friday:
        target_date = next_weekday(4)
    elif args.saturday:
        target_date = next_weekday(5)
    elif args.sunday:
        target_date = next_weekday(6)
    elif args.date:
        target_date = parse_date(args.date)
    else:
        target_date = today

    # ── Resolve timeFrom ──
    if args.after:
        h, m = parse_hhmm(args.after)
        time_from = f"{target_date}T{h:02d}:{m:02d}:00"
    elif target_date == today:
        now = datetime.now()
        time_from = f"{target_date}T{now.hour:02d}:{now.minute:02d}:00"
    else:
        # Future date: start from first trains of the day
        time_from = f"{target_date}T06:00:00"

    # ── Search ──
    data = api_search(args.from_station, args.to_station, time_from)

    if data is None:
        console.print("[yellow]No services found for this query.[/yellow]")
        return

    services = display_departures(data, args.from_station, args.to_station)

    # ── Detail view ──
    if args.detail is not None:
        idx = args.detail - 1
        if not services or idx < 0 or idx >= len(services):
            console.print(f"[red]No train #{args.detail} (found {len(services)} services)[/red]")
            sys.exit(1)

        svc = services[idx]
        sched_meta = svc.get("scheduleMetadata", {})
        identity = sched_meta.get("identity")
        dep_date = sched_meta.get("departureDate")

        if not identity or not dep_date:
            console.print("[red]Could not determine service identity from the listing.[/red]")
            sys.exit(1)

        console.print(f"\n[dim]Fetching detail for service {identity} on {dep_date}…[/dim]")
        detail = api_service(identity, dep_date)
        display_service_detail(detail, highlight_crs=args.to_station)


if __name__ == "__main__":
    main()
