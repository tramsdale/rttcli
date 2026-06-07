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


def save_route(name: str, leg1_from: str, leg1_to: str, leg2_from: str, leg2_to: str, transfer: int) -> None:
    cfg = load_config()
    cfg.setdefault("routes", {})[name] = {
        "legs": [[leg1_from.upper(), leg1_to.upper()], [leg2_from.upper(), leg2_to.upper()]],
        "transfer": transfer,
    }
    save_config(cfg)
    console.print(
        f"[green]Route '[bold]{name}[/bold]' saved:[/green] "
        f"{leg1_from.upper()}→{leg1_to.upper()}  +{transfer}min  {leg2_from.upper()}→{leg2_to.upper()}"
    )


def list_routes() -> None:
    routes = load_config().get("routes", {})
    if not routes:
        console.print("[yellow]No saved routes.[/yellow] Add one with: rtt config --add-route NAME FROM1 TO1 FROM2 TO2")
        return
    console.print("\n[bold]Saved routes:[/bold]")
    for name, r in routes.items():
        legs = r["legs"]
        transfer = r.get("transfer", 25)
        console.print(f"  [bold cyan]{name}[/bold cyan]  {legs[0][0]}→{legs[0][1]}  [dim]+{transfer}min[/dim]  {legs[1][0]}→{legs[1][1]}")


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


def parse_iso_naive(s: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a naive datetime (strips timezone)."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def get_terminus_arrival(svc: dict, target_crs: str) -> datetime | None:
    """Return arrival time at target_crs if it appears as the service's terminus destination."""
    for dest in svc.get("destination") or []:
        loc = dest.get("location", {})
        codes = (loc.get("shortCodes") or []) + (loc.get("longCodes") or [])
        if target_crs.upper() in [c.upper() for c in codes]:
            timing = dest.get("temporalData") or {}
            t = timing.get("scheduleAdvertised") or timing.get("scheduleInternal")
            return parse_iso_naive(t)
    return None


def find_arrival_at(service_data: dict, crs: str) -> datetime | None:
    """Return arrival datetime at crs from a service detail response (searches all locations)."""
    svc = service_data.get("service", service_data)
    for loc in svc.get("locations") or []:
        codes = (loc.get("location") or {}).get("shortCodes") or []
        if any(c.upper() == crs.upper() for c in codes):
            timing = (loc.get("temporalData") or {}).get("arrival") or {}
            t = timing.get("realtimeActual") or timing.get("realtimeForecast") or \
                timing.get("scheduleAdvertised") or timing.get("scheduleInternal")
            return parse_iso_naive(t)
    return None


def resolve_arrivals(services: list, to_crs: str) -> list[datetime | None]:
    """Return the arrival time at to_crs for each service, using service detail when needed."""
    arrivals = []
    for svc in services:
        arr = get_terminus_arrival(svc, to_crs)
        if arr is None:
            meta = svc.get("scheduleMetadata", {})
            identity, dep_date = meta.get("identity", ""), meta.get("departureDate", "")
            if identity and dep_date:
                arr = find_arrival_at(api_service(identity, dep_date), to_crs)
        arrivals.append(arr)
    return arrivals


def find_next_service_after(services: list, earliest: datetime) -> dict | None:
    """Return the first service whose scheduled departure is at or after earliest."""
    for svc in services:
        dep = (svc.get("temporalData") or {}).get("departure") or {}
        t_str = dep.get("realtimeActual") or dep.get("realtimeForecast") or \
                dep.get("scheduleAdvertised") or dep.get("scheduleInternal")
        t = parse_iso_naive(t_str)
        if t and t >= earliest:
            return svc
    return None


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

def display_departures(data: dict, from_crs: str, to_crs: str,
                       arrivals: list[datetime | None] | None = None) -> list:
    services = data.get("services") or []
    query = data.get("query", {})
    loc_name = query.get("location", {}).get("description", from_crs.upper())
    filter_to = query.get("filterTo", {})
    dest_name = filter_to.get("description", to_crs.upper())
    time_from = fmt_iso(query.get("timeFrom", ""))

    from_label = f"{loc_name} ({from_crs.upper()})"
    to_label = f"{dest_name} ({to_crs.upper()})"

    console.print()
    title = f"[bold]Trains from [cyan]{from_label}[/cyan] → [cyan]{to_label}[/cyan][/bold]"
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
    tbl.add_column(f"{to_crs.upper()} arr", width=8, min_width=5, no_wrap=True)
    tbl.add_column("Operator", width=20, min_width=12, no_wrap=True)
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
        _, dep_status = _time_status(dep_timing)
        # Show the actual/forecast time when available (status badge handles the +Nm detail)
        dep_time = fmt_iso(
            dep_timing.get("realtimeActual") or dep_timing.get("realtimeForecast")
            or dep_timing.get("scheduleAdvertised") or dep_timing.get("scheduleInternal")
        )
        dep_text = Text(
            dep_time or "-",
            style=_DEP_STYLES.get(dep_status, "") if not dep_timing.get("isCancelled") else "strike red",
        )

        badge = _status_badge(display_as, temporal, reasons)

        arr_dt = arrivals[i - 1] if arrivals and i - 1 < len(arrivals) else None
        arr_str = arr_dt.strftime("%H:%M") if arr_dt else "-"

        tbl.add_row(str(i), dep_text, headcode, dest, arr_str, operator, platform, badge)

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

        # Skip pure passes. When displayAs is null fall back to scheduledCallType
        # (far-future services often have null displayAs but a valid scheduledCallType)
        scheduled_call_type = temporal.get("scheduledCallType")
        is_advertised_call = scheduled_call_type in (
            "ADVERTISED_OPEN", "ADVERTISED_SET_DOWN", "ADVERTISED_PICK_UP"
        )
        if display_as == "PASS":
            continue
        if display_as is None and not is_advertised_call:
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


# ── Display: multi-leg route ──────────────────────────────────────────────────

_DEP_STYLES: dict[str, str] = {
    "cancelled": "strike red",
    "delayed": "yellow",
    "on_time": "green",
    "scheduled": "",
}


def display_route(route: dict, route_name: str, time_from: str, detail: int | None = None) -> None:
    legs = route["legs"]
    transfer_mins = route.get("transfer", 25)
    leg1_from, leg1_to = legs[0]
    leg2_from, leg2_to = legs[1]

    # ── Leg 1 search ──
    console.print(f"\n[dim]Searching {leg1_from}→{leg1_to}…[/dim]")
    leg1_data = api_search(leg1_from, leg1_to, time_from)
    if not leg1_data or not leg1_data.get("services"):
        console.print(f"[yellow]No {leg1_from}→{leg1_to} services found.[/yellow]")
        return

    leg1_services = leg1_data["services"]
    q1 = leg1_data.get("query", {})
    leg1_from_name = q1.get("location", {}).get("description", leg1_from)
    leg1_to_name   = q1.get("filterTo", {}).get("description", leg1_to)

    # ── For each leg 1 service, resolve arrival at interchange ──
    connections = []
    for svc in leg1_services[:5]:
        sched_meta = svc.get("scheduleMetadata", {})
        identity   = sched_meta.get("identity")
        dep_date   = sched_meta.get("departureDate")
        if not identity or not dep_date:
            continue

        # Try terminus shortcut first (avoids an API call when leg1_to is the train's terminus)
        arr = get_terminus_arrival(svc, leg1_to)
        if arr is None:
            svc_detail = api_service(identity, dep_date)
            arr = find_arrival_at(svc_detail, leg1_to)
        if arr is None:
            continue

        connections.append({
            "leg1_svc": svc,
            "leg1_arr": arr,
            "leg2_earliest": arr + timedelta(minutes=transfer_mins),
        })

    if not connections:
        console.print(f"[yellow]Could not resolve arrival times at {leg1_to}.[/yellow]")
        return

    # ── Leg 2 search from earliest possible departure ──
    earliest_leg2_dt = min(c["leg2_earliest"] for c in connections)
    leg2_time_from   = earliest_leg2_dt.strftime("%Y-%m-%dT%H:%M:%S")

    console.print(f"[dim]Searching {leg2_from}→{leg2_to} from {earliest_leg2_dt.strftime('%H:%M')}…[/dim]")
    leg2_data = api_search(leg2_from, leg2_to, leg2_time_from)
    leg2_services = (leg2_data.get("services") or []) if leg2_data else []

    q2 = (leg2_data or {}).get("query", {})
    leg2_from_name = q2.get("location", {}).get("description", leg2_from)
    leg2_to_name   = q2.get("filterTo", {}).get("description", leg2_to)

    # Match each connection to the next available leg 2 service, and resolve its arrival
    for conn in connections:
        s2 = find_next_service_after(leg2_services, conn["leg2_earliest"])
        conn["leg2_svc"] = s2
        if s2 is None:
            conn["leg2_arr"] = None
            continue
        arr2 = get_terminus_arrival(s2, leg2_to)
        if arr2 is None:
            s2_meta = s2.get("scheduleMetadata", {})
            detail2 = api_service(s2_meta.get("identity", ""), s2_meta.get("departureDate", ""))
            arr2 = find_arrival_at(detail2, leg2_to)
        conn["leg2_arr"] = arr2

    # ── Header ──
    console.print()
    console.print(
        f"[bold]Route [cyan]{route_name}[/cyan]  "
        f"{leg1_from_name} ({leg1_from}) → {leg1_to_name} ({leg1_to})  "
        f"[dim]+{transfer_mins}min[/dim]  "
        f"{leg2_from_name} ({leg2_from}) → {leg2_to_name} ({leg2_to})[/bold]"
    )
    console.print()

    # ── Connection table ──
    tbl = Table(box=box.ROUNDED, show_header=True, header_style="bold blue", padding=(0, 1), expand=False)
    tbl.add_column("#",             style="dim", width=3,  min_width=3, justify="right", no_wrap=True)
    tbl.add_column(f"{leg1_from} dep", width=8, min_width=5, no_wrap=True)
    tbl.add_column("Code",          width=6,  min_width=4, no_wrap=True)
    tbl.add_column(f"{leg1_to} arr",  width=8, min_width=5, no_wrap=True)
    tbl.add_column(f"{leg2_from} dep", width=9, min_width=5, no_wrap=True)
    tbl.add_column("Code",          width=6,  min_width=4, no_wrap=True)
    tbl.add_column(f"{leg2_to} arr",  width=8, min_width=5, no_wrap=True)
    tbl.add_column("Status",        width=22, min_width=8)

    for i, conn in enumerate(connections, 1):
        s1 = conn["leg1_svc"]
        s2 = conn.get("leg2_svc")

        dep1 = (s1.get("temporalData") or {}).get("departure") or {}
        dep1_str, dep1_status = _time_status(dep1)
        code1 = s1.get("scheduleMetadata", {}).get("trainReportingIdentity", "-")
        arr1_str = conn["leg1_arr"].strftime("%H:%M")

        if s2:
            dep2 = (s2.get("temporalData") or {}).get("departure") or {}
            dep2_str, dep2_status = _time_status(dep2)
            code2    = s2.get("scheduleMetadata", {}).get("trainReportingIdentity", "-")
            arr2_str = conn["leg2_arr"].strftime("%H:%M") if conn.get("leg2_arr") else "-"
            badge    = _status_badge(
                (s2.get("temporalData") or {}).get("displayAs", "CALL"),
                s2.get("temporalData") or {},
                s2.get("reasons") or [],
            )
            dep2_text = Text(dep2_str, style=_DEP_STYLES.get(dep2_status, ""))
        else:
            dep2_text = Text("No service", style="red")
            code2     = "-"
            arr2_str  = "-"
            badge     = Text("No connection", style="bold red")

        tbl.add_row(
            str(i),
            Text(dep1_str, style=_DEP_STYLES.get(dep1_status, "")),
            code1,
            arr1_str,
            dep2_text,
            code2,
            arr2_str,
            badge,
        )

    console.print(tbl)
    console.print(
        f"[dim]  Transfer: {transfer_mins} min at {leg1_to_name}  ·  "
        f"Use  rtt {leg1_from} {leg1_to}  or  rtt {leg2_from} {leg2_to}  for individual legs[/dim]"
    )

    # ── Optional detail view for a specific connection ──
    if detail is not None:
        idx = detail - 1
        if idx < 0 or idx >= len(connections):
            console.print(f"[red]No connection #{detail} (found {len(connections)})[/red]")
            return
        conn = connections[idx]
        s1 = conn["leg1_svc"]
        s2 = conn.get("leg2_svc")

        m1 = s1.get("scheduleMetadata", {})
        console.print(f"\n[dim]Leg 1 detail: {m1.get('identity')} on {m1.get('departureDate')}…[/dim]")
        display_service_detail(api_service(m1["identity"], m1["departureDate"]), highlight_crs=leg1_to)

        if s2:
            m2 = s2.get("scheduleMetadata", {})
            console.print(f"\n[dim]Leg 2 detail: {m2.get('identity')} on {m2.get('departureDate')}…[/dim]")
            display_service_detail(api_service(m2["identity"], m2["departureDate"]), highlight_crs=leg2_to)
        else:
            console.print("[yellow]No leg 2 service to show detail for.[/yellow]")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    # Handle `rtt config …` before argparse sees positional args
    if len(sys.argv) >= 2 and sys.argv[1] == "config":
        cfg_p = argparse.ArgumentParser(prog="rtt config")
        grp = cfg_p.add_mutually_exclusive_group(required=True)
        grp.add_argument("--token", metavar="TOKEN", help="RTT API Bearer token")
        grp.add_argument(
            "--add-route",
            nargs=5,
            metavar=("NAME", "FROM1", "TO1", "FROM2", "TO2"),
            help="Save a two-leg route alias",
        )
        grp.add_argument("--list-routes", action="store_true", help="List saved routes")
        cfg_p.add_argument("--transfer", type=int, default=25, metavar="MINS",
                           help="Transfer time in minutes (default: 25)")
        a = cfg_p.parse_args(sys.argv[2:])
        if a.token:
            save_token(a.token)
        elif a.add_route:
            name, f1, t1, f2, t2 = a.add_route
            save_route(name, f1, t1, f2, t2, a.transfer)
        else:
            list_routes()
        return

    parser = argparse.ArgumentParser(
        prog="rtt",
        description="Real Time Trains CLI  —  rtt FROM TO [options]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  rtt config --token eyJ...                    save your API token
  rtt config --add-route rtn BRI PAD KGX CBG   save a two-leg route
  rtt config --list-routes                     list saved routes
  rtt PAD BRI                                  next trains Paddington → Bristol
  rtt PAD BRI --after 2100                     trains after 21:00
  rtt PAD BRI --tomorrow                       tomorrow's trains
  rtt PAD BRI --friday                         next Friday's trains
  rtt PAD BRI --date 7/6/26                    specific date
  rtt PAD BRI --detail 1                       full calling points for first train
  rtt rtn                                      run saved two-leg route
  rtt rtn --tomorrow --after 0700
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

    # Detect named route: `rtt rtn` or `rtt rtn --tomorrow` etc.
    saved_routes = load_config().get("routes", {})
    is_route = bool(args.from_station and not args.to_station and args.from_station in saved_routes)

    if not is_route and (not args.from_station or not args.to_station):
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

    # ── Route or direct search ──
    if is_route:
        display_route(saved_routes[args.from_station], args.from_station, time_from, detail=args.detail)
        return

    data = api_search(args.from_station, args.to_station, time_from)

    if data is None:
        console.print("[yellow]No services found for this query.[/yellow]")
        return

    raw_services = data.get("services") or []
    arrivals = resolve_arrivals(raw_services, args.to_station)
    services = display_departures(data, args.from_station, args.to_station, arrivals)

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
