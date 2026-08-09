"""
RTT MCP + REST server — deployable to AWS Lambda via Zappa.

MCP endpoint:  POST /mcp   (Claude / any MCP client)
REST endpoints: GET /api/trains, /api/service, /api/route  (Custom GPT / any HTTP client)
OpenAPI spec:  GET /openapi.json  (paste URL into Custom GPT action setup)
HTML page:     GET /t/<identity>/<date>  (public, no-auth live train tracker — shareable link)

Environment variables:
    RTT_TOKEN   — your Real Time Trains API bearer token
"""
import html
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests as http
from flask import Flask, jsonify, request

# Allow importing rtt.py from the parent directory (local dev).
# When deployed via Zappa from the repo root, rtt.py is packaged alongside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rtt  # noqa: E402


# ── Lambda token support ───────────────────────────────────────────────────────
# When RTT_TOKEN env var is set, bypass the CLI's file-based config and cache
# the access token in-process (reused across warm Lambda invocations).

_token_cache: dict = {}


def _get_access_token_env() -> str:
    refresh = os.environ["RTT_TOKEN"]

    cached = _token_cache.get("access_token")
    valid_str = _token_cache.get("valid_until", "")
    if cached and valid_str:
        try:
            valid_until = datetime.fromisoformat(valid_str.replace("Z", "+00:00"))
            if datetime.now(tz=valid_until.tzinfo) < valid_until - timedelta(seconds=30):
                return cached
        except ValueError:
            pass

    r = http.get(
        f"{rtt.BASE_URL}/api/get_access_token",
        headers={"Authorization": f"Bearer {refresh}"},
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    data = r.json()
    _token_cache["access_token"] = data["token"]
    _token_cache["valid_until"] = data.get("validUntil", "")
    return data["token"]


if os.environ.get("RTT_TOKEN"):
    rtt.get_access_token = _get_access_token_env


app = Flask(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _parse_date(s: str | None) -> date:
    if not s:
        return date.today()
    for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Invalid date {s!r} — use YYYY-MM-DD")


def _parse_hhmm(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    clean = s.replace(":", "").zfill(4)
    if len(clean) == 4 and clean.isdigit():
        h, m = int(clean[:2]), int(clean[2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    raise ValueError(f"Invalid time {s!r} — use HHMM or HH:MM")


def _build_time_from(target_date: date, after: str | None, arriveby_dt: datetime | None,
                     is_route: bool = False) -> str:
    today = date.today()
    if after:
        hm = _parse_hhmm(after)
        return f"{target_date}T{hm[0]:02d}:{hm[1]:02d}:00"
    if arriveby_dt:
        lookback = timedelta(hours=6) if is_route else timedelta(hours=4)
        window_start = arriveby_dt - lookback
        if target_date == today:
            window_start = max(window_start, datetime.now())
        return window_start.strftime("%Y-%m-%dT%H:%M:%S")
    if target_date == today:
        now = datetime.now()
        return f"{target_date}T{now.hour:02d}:{now.minute:02d}:00"
    return f"{target_date}T06:00:00"


def _svc_summary(svc: dict, arr: datetime | None, idx: int) -> dict:
    """Flatten a raw RTT service dict into a clean response object."""
    dep = (svc.get("temporalData") or {}).get("departure") or {}
    meta = svc.get("scheduleMetadata") or {}
    dests = svc.get("destination") or [{}]
    dest_name = dests[0].get("location", {}).get("description", "-") if dests else "-"

    sched   = (dep.get("scheduleAdvertised") or dep.get("scheduleInternal") or "")[11:16]
    actual  = (dep.get("realtimeActual") or "")[11:16]
    forecast = (dep.get("realtimeForecast") or "")[11:16]
    lateness = dep.get("realtimeAdvertisedLateness") or 0
    cancelled = dep.get("isCancelled", False)

    if cancelled:
        departs, status = sched, "Cancelled"
    elif actual:
        departs = actual
        status = f"Delayed +{lateness}m" if lateness and lateness > 1 else "On time"
    else:
        departs = sched
        status = f"Expected late +{lateness}m" if (forecast and lateness and lateness > 1) else "Scheduled"

    plat_obj = ((svc.get("locationMetadata") or {}).get("platform") or {})
    platform = plat_obj.get("actual") or plat_obj.get("planned") or "-"

    reasons = svc.get("reasons") or []
    out = {
        "index": idx,
        "headcode": meta.get("trainReportingIdentity", "-"),
        "identity": meta.get("identity", ""),
        "departure_date": meta.get("departureDate", ""),
        "departs": departs,
        "destination": dest_name,
        "operator": meta.get("atocName", "-"),
        "platform": platform,
        "status": status,
    }
    if reasons:
        out["delay_reason"] = reasons[0].get("shortText", "")
    if arr:
        out["arrives"] = arr.strftime("%H:%M")
    return out


# ── Core logic (shared between MCP tools and REST endpoints) ───────────────────

def _search_trains(from_crs: str, to_crs: str, date_str: str | None,
                   after: str | None, arriveby: str | None) -> dict:
    target_date = _parse_date(date_str)
    arriveby_dt = None
    if arriveby:
        hm = _parse_hhmm(arriveby)
        if hm:
            arriveby_dt = datetime(target_date.year, target_date.month, target_date.day, *hm)

    time_from = _build_time_from(target_date, after, arriveby_dt)

    # 120-min window is enough for a typical departure board; arriveby queries use the
    # full window so the lookback already constrains how far back we search.
    window = 240 if arriveby_dt else 120
    try:
        data = rtt.api_search(from_crs, to_crs, time_from, time_window=window)
    except SystemExit as e:
        raise RuntimeError("RTT API error") from e

    if not data:
        return {"from": from_crs.upper(), "to": to_crs.upper(), "date": str(target_date), "trains": []}

    query = data.get("query", {})
    from_name = query.get("location", {}).get("description", from_crs.upper())
    to_name   = query.get("filterTo", {}).get("description", to_crs.upper())
    raw = data.get("services") or []

    try:
        arrivals = rtt.resolve_arrivals(raw, to_crs)
    except SystemExit:
        arrivals = [None] * len(raw)

    if arriveby_dt:
        pairs, after_count = [], 0
        for svc, arr in zip(raw, arrivals):
            if arr is not None and arr <= arriveby_dt:
                pairs.append((svc, arr))
            elif after_count < 2:
                pairs.append((svc, arr))
                after_count += 1
        raw, arrivals = ([s for s, _ in pairs], [a for _, a in pairs]) if pairs else ([], [])

    return {
        "from": f"{from_name} ({from_crs.upper()})",
        "to":   f"{to_name} ({to_crs.upper()})",
        "date": str(target_date),
        "trains": [_svc_summary(s, a, i + 1) for i, (s, a) in enumerate(zip(raw, arrivals))],
    }


def _get_service(identity: str, dep_date: str, from_crs: str | None, to_crs: str | None,
                 include_passing: bool = False) -> dict:
    try:
        data = rtt.api_service(identity, dep_date, detailed=include_passing)
    except SystemExit as e:
        raise RuntimeError("RTT API error") from e

    svc = data.get("service", data)
    meta = svc.get("scheduleMetadata") or {}
    origins = svc.get("origin") or [{}]
    dests   = svc.get("destination") or [{}]

    calling_points = []
    for loc in svc.get("locations") or []:
        temporal = loc.get("temporalData") or {}
        display_as = temporal.get("displayAs")
        sct = temporal.get("scheduledCallType")
        is_advertised = sct in ("ADVERTISED_OPEN", "ADVERTISED_SET_DOWN", "ADVERTISED_PICK_UP")
        is_pass = display_as == "PASS"
        if is_pass and not include_passing:
            continue
        if display_as is None and not is_advertised and not is_pass:
            continue

        location = loc.get("location", {})
        codes = location.get("shortCodes") or []

        def _t(timing: dict) -> str | None:
            s = (timing.get("realtimeActual") or timing.get("realtimeForecast") or
                 timing.get("scheduleAdvertised") or timing.get("scheduleInternal") or "")[11:16]
            return s or None

        if is_pass:
            pass_timing = temporal.get("pass") or {}
            arrives, departs = None, _t(pass_timing)
            lateness = pass_timing.get("realtimeInternalLateness") or 0
        else:
            arrives  = _t(temporal.get("arrival")   or {})
            departs  = _t(temporal.get("departure")  or {})
            lateness = 0

        plat_obj = ((loc.get("locationMetadata") or {}).get("platform") or {})
        platform = plat_obj.get("actual") or plat_obj.get("forecast") or plat_obj.get("planned") or "-"

        point: dict = {
            "station":  location.get("description", "-"),
            "crs":      codes[0] if codes else "-",
            "arrives":  arrives,
            "departs":  departs,
            "platform": platform,
            "status":   display_as or "CALL",
        }
        if is_pass and lateness and lateness > 1:
            point["lateness_mins"] = lateness
        if from_crs and any(c.upper() == from_crs.upper() for c in codes):
            point["note"] = "board here"
        if to_crs and any(c.upper() == to_crs.upper() for c in codes):
            point["note"] = "alight here"
        calling_points.append(point)

    return {
        "headcode":      meta.get("trainReportingIdentity", "-"),
        "identity":      identity,
        "date":          dep_date,
        "origin":        (origins[0].get("location") or {}).get("description", "-"),
        "destination":   (dests[0].get("location") or {}).get("description", "-"),
        "operator":      meta.get("atocName", "-"),
        "calling_points": calling_points,
    }


def _search_route(from1: str, to1: str, from2: str, to2: str, transfer_mins: int,
                  date_str: str | None, after: str | None, arriveby: str | None) -> dict:
    target_date = _parse_date(date_str)
    arriveby_dt = None
    if arriveby:
        hm = _parse_hhmm(arriveby)
        if hm:
            arriveby_dt = datetime(target_date.year, target_date.month, target_date.day, *hm)

    time_from = _build_time_from(target_date, after, arriveby_dt, is_route=True)

    try:
        leg1_data = rtt.api_search(from1, to1, time_from)
    except SystemExit as e:
        raise RuntimeError("RTT API error on leg 1") from e

    if not leg1_data or not leg1_data.get("services"):
        raise RuntimeError(f"No services found for {from1}→{to1}")

    q1 = leg1_data.get("query", {})
    from1_name = q1.get("location", {}).get("description", from1.upper())
    to1_name   = q1.get("filterTo", {}).get("description", to1.upper())

    leg1_svcs = leg1_data["services"]
    limit = len(leg1_svcs) if arriveby_dt else 5

    connections = []
    for svc in leg1_svcs[:limit]:
        meta = svc.get("scheduleMetadata", {})
        identity, dep_date = meta.get("identity"), meta.get("departureDate")
        if not identity or not dep_date:
            continue
        arr = rtt.get_terminus_arrival(svc, to1)
        if arr is None:
            try:
                arr = rtt.find_arrival_at(rtt.api_service(identity, dep_date), to1)
            except SystemExit:
                continue
        if arr is None:
            continue
        connections.append({"leg1_svc": svc, "leg1_arr": arr,
                             "leg2_earliest": arr + timedelta(minutes=transfer_mins)})

    if not connections:
        raise RuntimeError(f"Could not resolve arrivals at {to1}")

    earliest_dt = min(c["leg1_arr"] for c in connections)
    leg2_window = max(240, int((arriveby_dt - earliest_dt).total_seconds() // 60) + 120) \
                  if arriveby_dt else 240

    try:
        leg2_data = rtt.api_search(from2, to2, earliest_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                                   time_window=leg2_window)
    except SystemExit as e:
        raise RuntimeError("RTT API error on leg 2") from e

    leg2_svcs = (leg2_data.get("services") or []) if leg2_data else []
    q2 = (leg2_data or {}).get("query", {})
    from2_name = q2.get("location", {}).get("description", from2.upper())
    to2_name   = q2.get("filterTo", {}).get("description", to2.upper())

    for conn in connections:
        # First leg-2 service after leg-1 arrives (may be too soon to catch)
        candidate = rtt.find_next_service_after(leg2_svcs, conn["leg1_arr"])
        # First leg-2 service catchable after the transfer window
        s2 = rtt.find_next_service_after(leg2_svcs, conn["leg2_earliest"])
        # If there's a service between arrival and earliest-catchable, it's the one just missed
        conn["missed_s2"] = candidate if (candidate and candidate is not s2) else None
        conn["leg2_svc"] = s2
        if s2 is None:
            conn["leg2_arr"] = None
            continue
        arr2 = rtt.get_terminus_arrival(s2, to2)
        if arr2 is None:
            m = s2.get("scheduleMetadata", {})
            try:
                arr2 = rtt.find_arrival_at(rtt.api_service(m.get("identity", ""), m.get("departureDate", "")), to2)
            except SystemExit:
                pass
        conn["leg2_arr"] = arr2

    if arriveby_dt:
        filtered, after_count = [], 0
        for conn in connections:
            arr2 = conn.get("leg2_arr")
            if arr2 is not None and arr2 <= arriveby_dt:
                filtered.append(conn)
            elif after_count < 2:
                filtered.append(conn)
                after_count += 1
        connections = filtered

    result_conns = []
    for i, conn in enumerate(connections, 1):
        s1  = conn["leg1_svc"]
        s2  = conn.get("leg2_svc")
        m1  = s1.get("scheduleMetadata", {})
        d1  = (s1.get("temporalData") or {}).get("departure") or {}
        dep1 = (d1.get("realtimeActual") or d1.get("realtimeForecast") or
                d1.get("scheduleAdvertised") or "")[11:16]

        c: dict = {
            "index": i,
            "leg1_departs": dep1,
            "leg1_headcode": m1.get("trainReportingIdentity", "-"),
            "interchange_arrives": conn["leg1_arr"].strftime("%H:%M"),
        }

        ms2 = conn.get("missed_s2")
        if ms2:
            dm  = (ms2.get("temporalData") or {}).get("departure") or {}
            dep_missed = (dm.get("realtimeActual") or dm.get("realtimeForecast") or
                          dm.get("scheduleAdvertised") or "")[11:16]
            c["missed_leg2"] = {
                "departs":  dep_missed,
                "headcode": ms2.get("scheduleMetadata", {}).get("trainReportingIdentity", "-"),
                "note":     "just missed — departs before transfer window",
            }

        if s2:
            m2  = s2.get("scheduleMetadata", {})
            d2  = (s2.get("temporalData") or {}).get("departure") or {}
            dep2 = (d2.get("realtimeActual") or d2.get("realtimeForecast") or
                    d2.get("scheduleAdvertised") or "")[11:16]
            dep2_dt = rtt.parse_iso_naive(
                d2.get("realtimeActual") or d2.get("realtimeForecast") or d2.get("scheduleAdvertised"))
            total = int((dep2_dt - conn["leg1_arr"]).total_seconds() // 60) if dep2_dt else None
            margin = (total - transfer_mins) if total is not None else None
            c.update({
                "leg2_departs":        dep2,
                "leg2_headcode":       m2.get("trainReportingIdentity", "-"),
                "final_arrives":       conn["leg2_arr"].strftime("%H:%M") if conn.get("leg2_arr") else None,
                "margin_mins":         margin,
                "total_transfer_mins": total,
                "status": (s2.get("temporalData") or {}).get("displayAs", "SCHEDULED"),
            })
        else:
            c["status"] = "NO_CONNECTION"

        result_conns.append(c)

    return {
        "leg1": f"{from1_name} ({from1.upper()}) → {to1_name} ({to1.upper()})",
        "leg2": f"{from2_name} ({from2.upper()}) → {to2_name} ({to2.upper()})",
        "transfer_mins": transfer_mins,
        "date": str(target_date),
        "connections": result_conns,
    }


# ── Train tracker HTML page (public, no-auth, shareable link) ──────────────────

_STATUS_COLORS = {
    "cancelled":  "#dc2626",
    "diverted":   "#9333ea",
    "starts":     "#0891b2",
    "terminates": "#0891b2",
    "delayed":    "#d97706",
    "on_time":    "#16a34a",
    "scheduled":  "#6b7280",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _best_time_str(timing: dict) -> str | None:
    """HH:MM using the best available timing, independent of display formatting."""
    t = (timing.get("realtimeActual") or timing.get("realtimeForecast") or
         timing.get("scheduleAdvertised") or timing.get("scheduleInternal") or "")
    return t[11:16] or None


def _calling_point_rows(svc_data: dict, from_crs: str | None, to_crs: str | None,
                        include_passing: bool = False) -> list[dict]:
    svc = svc_data.get("service", svc_data)
    rows = []
    for loc in svc.get("locations") or []:
        temporal = loc.get("temporalData") or {}
        display_as = temporal.get("displayAs")
        sct = temporal.get("scheduledCallType")
        is_advertised = sct in ("ADVERTISED_OPEN", "ADVERTISED_SET_DOWN", "ADVERTISED_PICK_UP")
        is_pass = display_as == "PASS"
        if is_pass and not include_passing:
            continue
        if display_as is None and not is_advertised and not is_pass:
            continue

        location = loc.get("location", {})
        codes = location.get("shortCodes") or []
        reasons = loc.get("reasons") or []

        if is_pass:
            timing = temporal.get("pass") or {}
            arr_disp, arr_status = "-", "scheduled"
            dep_disp, dep_status = rtt.time_status(timing)
            sort_time = _best_time_str(timing)
        else:
            arr_timing = temporal.get("arrival") or {}
            dep_timing = temporal.get("departure") or {}
            arr_disp, arr_status = rtt.time_status(arr_timing)
            dep_disp, dep_status = rtt.time_status(dep_timing)
            sort_time = _best_time_str(dep_timing) or _best_time_str(arr_timing)

        plat_obj = ((loc.get("locationMetadata") or {}).get("platform") or {})
        platform = plat_obj.get("actual") or plat_obj.get("forecast") or plat_obj.get("planned") or "-"

        label, style = rtt.status_badge_info(display_as, temporal, reasons)
        codes_upper = [c.upper() for c in codes]

        rows.append({
            "station":        location.get("description", "-"),
            "crs":            codes[0] if codes else "-",
            "is_pass":        is_pass,
            "arrives":        arr_disp,
            "arrives_status": arr_status,
            "departs":        dep_disp,
            "departs_status": dep_status,
            "sort_time":      sort_time,
            "platform":       platform,
            "status_label":   label,
            "status_style":   style,
            "board":          bool(from_crs and from_crs.upper() in codes_upper),
            "alight":         bool(to_crs and to_crs.upper() in codes_upper),
        })
    return rows


def _badge_html(label: str, style: str) -> str:
    color = _STATUS_COLORS.get(style, "#6b7280")
    return f'<span class="badge" style="background:{color}">{_esc(label)}</span>'


def _error_html(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 480px; margin: 80px auto; padding: 0 16px; text-align: center; color: #334155; }}
  h1 {{ font-size: 1.2rem; }}
</style></head>
<body><h1>{_esc(title)}</h1><p>{_esc(message)}</p></body></html>"""


def _train_tracker_html(identity: str, dep_date: str, from_crs: str | None, to_crs: str | None,
                        refresh: int) -> str:
    # Passing points aren't shown on this page, so no need for the (more heavily
    # rate-limited) "additional detail" tier — same data as the plain REST lookup.
    data = rtt.api_service(identity, dep_date, detailed=False)
    svc = data.get("service", data)
    meta = svc.get("scheduleMetadata") or {}
    origins = svc.get("origin") or [{}]
    dests = svc.get("destination") or [{}]

    headcode    = meta.get("trainReportingIdentity", "-")
    operator    = (meta.get("operator") or {}).get("name", "-") or "-"
    origin_name = (origins[0].get("location") or {}).get("description", "-")
    dest_name   = (dests[0].get("location") or {}).get("description", "-")

    rows = _calling_point_rows(data, from_crs, to_crs)

    is_today = dep_date == str(date.today())
    now_str = datetime.now().strftime("%H:%M") if is_today else None
    next_idx = None
    if now_str:
        for i, r in enumerate(rows):
            if r["sort_time"] and r["sort_time"] >= now_str:
                next_idx = i
                break

    target = [r for r in rows if r["alight"]] or rows[-1:]
    overall_label, overall_style = (
        (target[0]["status_label"], target[0]["status_style"]) if target else ("Scheduled", "scheduled")
    )

    row_html = []
    for i, r in enumerate(rows):
        classes = ["stop"]
        if i == next_idx:
            classes.append("next")
        elif next_idx is not None and i < next_idx:
            classes.append("passed")

        marker = "📍 " if i == next_idx else ""
        tag = ""
        if r["board"]:
            tag = ' <span class="tag">board</span>'
        elif r["alight"]:
            tag = ' <span class="tag">alight</span>'

        arr_html = f'<div class="arr">arr {_esc(r["arrives"])}</div>' if r["arrives"] not in ("-", None) else ""

        row_html.append(f"""
        <div class="{' '.join(classes)}">
          <div class="stop-time">
            <div class="dep">{_esc(r['departs'])}</div>
            {arr_html}
          </div>
          <div class="stop-main">
            <div class="stop-name">{marker}{_esc(r['station'])}{tag}</div>
            <div class="stop-meta">Plat {_esc(r['platform'])}</div>
          </div>
          <div class="stop-status">{_badge_html(r['status_label'], r['status_style'])}</div>
        </div>""")

    refresh_meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else ""
    updated = datetime.now().strftime("%H:%M:%S")

    toggle_params = {}
    if from_crs:
        toggle_params["from"] = from_crs
    if to_crs:
        toggle_params["to"] = to_crs
    toggle_params["refresh"] = 0 if refresh > 0 else 30
    toggle_url = "?" + urlencode(toggle_params)
    toggle_label = "pause auto-refresh" if refresh > 0 else "resume auto-refresh"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_meta}
<title>{_esc(headcode)} {_esc(origin_name)} → {_esc(dest_name)} — Train Tracker</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 16px; max-width: 560px; margin-inline: auto;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f8fafc; color: #0f172a;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0f172a; color: #e2e8f0; }}
    .card {{ background: #1e293b !important; border-color: #334155 !important; }}
    .stop {{ border-color: #334155 !important; }}
    .stop-meta {{ color: #94a3b8 !important; }}
    .tag {{ background: #334155 !important; }}
    .next {{ background: rgba(96,165,250,0.15) !important; }}
  }}
  .card {{
    background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
    padding: 16px; margin-bottom: 12px;
  }}
  h1 {{ font-size: 1.1rem; margin: 0 0 4px; }}
  .route {{ font-size: 0.95rem; opacity: 0.8; margin-bottom: 8px; }}
  .badge {{
    display: inline-block; color: #fff; font-size: 0.8rem; font-weight: 600;
    padding: 3px 10px; border-radius: 999px; white-space: nowrap;
  }}
  .stop {{
    display: flex; align-items: center; gap: 12px; padding: 10px 0;
    border-bottom: 1px solid #e2e8f0;
  }}
  .stop:last-child {{ border-bottom: none; }}
  .stop-time {{ width: 64px; flex-shrink: 0; text-align: right; font-variant-numeric: tabular-nums; }}
  .stop-time .dep {{ font-weight: 600; }}
  .stop-time .arr {{ font-size: 0.75rem; opacity: 0.7; }}
  .stop-main {{ flex: 1; min-width: 0; }}
  .stop-name {{ font-weight: 500; }}
  .stop-meta {{ font-size: 0.8rem; color: #64748b; }}
  .stop-status {{ flex-shrink: 0; }}
  .passed {{ opacity: 0.45; }}
  .next {{ background: rgba(37,99,235,0.08); border-radius: 8px; margin: 0 -8px; padding: 10px 8px; }}
  .tag {{
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
    background: #e2e8f0; padding: 1px 6px; border-radius: 4px; margin-left: 6px;
  }}
  footer {{ font-size: 0.8rem; opacity: 0.6; text-align: center; margin-top: 16px; }}
  footer a {{ color: inherit; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{_esc(headcode)} · {_esc(operator)}</h1>
    <div class="route">{_esc(origin_name)} → {_esc(dest_name)} · {_esc(dep_date)}</div>
    <div class="overall">{_badge_html(overall_label, overall_style)}</div>
  </div>
  <div class="card">
    {''.join(row_html) if row_html else '<p>No calling points found for this service.</p>'}
  </div>
  <footer>
    Updated {updated}{f' · refreshes every {refresh}s' if refresh > 0 else ' · auto-refresh off'}
    · <a href="{_esc(toggle_url)}">{toggle_label}</a>
  </footer>
</body>
</html>"""


@app.route("/t/<identity>/<dep_date>")
def train_tracker(identity, dep_date):
    from_crs = (request.args.get("from") or "").strip() or None
    to_crs   = (request.args.get("to") or "").strip() or None
    try:
        # Off by default — auto-refresh must be turned on explicitly (?refresh=30)
        # so an open tab doesn't keep polling the RTT API unattended.
        refresh = int(request.args.get("refresh", 0))
    except ValueError:
        refresh = 0
    refresh = max(0, min(refresh, 300))

    try:
        _parse_date(dep_date)
    except ValueError:
        return _error_html("Invalid date", f"{dep_date!r} is not a valid date — use YYYY-MM-DD."), 400

    try:
        page = _train_tracker_html(identity, dep_date, from_crs, to_crs, refresh)
    except SystemExit:
        return _error_html(
            "Train not found",
            "Could not find this service — it may not run on that date, or the link may be incorrect.",
        ), 404
    except Exception as e:
        return _error_html("Something went wrong", f"{type(e).__name__}: {e}"), 500

    return page, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/trains")
def rest_trains():
    try:
        from_crs = request.args.get("from", "").strip()
        to_crs   = request.args.get("to", "").strip()
        if not from_crs or not to_crs:
            return jsonify({"error": "from and to query parameters are required"}), 400
        return jsonify(_search_trains(from_crs, to_crs,
                                      request.args.get("date"),
                                      request.args.get("after"),
                                      request.args.get("arriveby")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (RuntimeError, SystemExit) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {type(e).__name__}: {e}"}), 500


@app.route("/api/service")
def rest_service():
    try:
        identity = request.args.get("identity", "").strip()
        dep_date = request.args.get("date", "").strip()
        if not identity or not dep_date:
            return jsonify({"error": "identity and date query parameters are required"}), 400
        include_passing = request.args.get("passing", "false").lower() == "true"
        return jsonify(_get_service(identity, dep_date,
                                    from_crs=request.args.get("from"),
                                    to_crs=request.args.get("to"),
                                    include_passing=include_passing))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (RuntimeError, SystemExit) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {type(e).__name__}: {e}"}), 500


@app.route("/api/route")
def rest_route():
    try:
        from1 = request.args.get("from1", "").strip()
        to1   = request.args.get("to1",   "").strip()
        from2 = request.args.get("from2", "").strip()
        to2   = request.args.get("to2",   "").strip()
        if not all([from1, to1, from2, to2]):
            return jsonify({"error": "from1, to1, from2, to2 are required"}), 400
        transfer = int(request.args.get("transfer", 25))
        return jsonify(_search_route(from1, to1, from2, to2, transfer,
                                     request.args.get("date"),
                                     request.args.get("after"),
                                     request.args.get("arriveby")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except (RuntimeError, SystemExit) as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {type(e).__name__}: {e}"}), 500


# ── MCP endpoint (Streamable HTTP, protocol version 2024-11-05) ────────────────

_MCP_TOOLS = [
    {
        "name": "search_trains",
        "description": (
            "Search for UK train services between two stations on a SINGLE rail leg. "
            "Returns departure times, arrival times, headcodes, platforms, operators, and live status. "
            "IMPORTANT: do NOT use this for journeys that cross London between different terminals "
            "(e.g. Bristol→Cambridge, Exeter→Norwich, Cardiff→Leeds). "
            "For those, use search_route instead — it handles the cross-London transfer automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_crs":  {"type": "string", "description": "Origin CRS code (e.g. PAD, BRI, KGX)"},
                "to_crs":    {"type": "string", "description": "Destination CRS code"},
                "date":      {"type": "string", "description": "Date YYYY-MM-DD (default: today)"},
                "after":     {"type": "string", "description": "Show trains departing at or after HHMM (e.g. 0900)"},
                "arriveby":  {"type": "string", "description": "Show trains arriving by HHMM"},
            },
            "required": ["from_crs", "to_crs"],
        },
    },
    {
        "name": "get_service_detail",
        "description": (
            "Get all calling points for a specific train service, including scheduled and actual "
            "arrival/departure times, platforms, and delay reasons. Use the identity and "
            "departure_date from a search_trains result. "
            "By default only scheduled stops are returned. Set include_passing=true to also "
            "return every intermediate location the train passes through without stopping — "
            "this gives a complete picture of the train's path and current position. "
            "Always set include_passing=true when the user asks about passing points, "
            "intermediate stations, or where the train currently is between stops."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identity":        {"type": "string",  "description": "Service identity (e.g. W34739)"},
                "departure_date":  {"type": "string",  "description": "Date YYYY-MM-DD"},
                "from_crs":        {"type": "string",  "description": "Boarding station CRS (optional)"},
                "to_crs":          {"type": "string",  "description": "Alighting station CRS (optional)"},
                "include_passing": {"type": "boolean", "description": "Set to true to include intermediate passing points (non-stopping locations). Required for complete train path detail."},
            },
            "required": ["identity", "departure_date"],
        },
    },
    {
        "name": "search_route",
        "description": (
            "Find connections for a two-leg rail journey with an interchange. "
            "Use this whenever a journey requires changing between London terminals or any other "
            "station-to-station transfer that is not itself a train (e.g. Tube, walking, bus). "
            "The tool searches leg 1 trains, resolves arrivals at the interchange, then finds "
            "the first viable leg 2 train after the transfer window. "
            "transfer_mins defaults to 25 and should be kept at 25 unless you have a specific reason. "
            "\n\n"
            "CROSS-LONDON ROUTING — use this tool whenever a journey passes through London "
            "and the two rail legs use different terminals. The transfer leg (Tube etc.) is "
            "not searched; transfer_mins covers it. Default transfer times between London terminals:\n"
            "  PAD  → KGX/STP : 25 min (Circle/H&C line or Tube)\n"
            "  PAD  → EUS      : 25 min (Circle/H&C line)\n"
            "  PAD  → WAT      : 30 min (Bakerloo then walk, or bus)\n"
            "  PAD  → VIC      : 25 min (Circle/District line)\n"
            "  PAD  → LBG      : 30 min (Circle line)\n"
            "  KGX  → STP      : 5  min (adjacent stations, short walk; use 10 to be safe)\n"
            "  KGX  → EUS      : 10 min (short walk)\n"
            "  VIC  → WAT      : 20 min (walk or bus)\n"
            "  VIC  → LBG      : 15 min (Circle/District line)\n"
            "  WAT  → LBG      : 15 min (Jubilee or walk)\n"
            "\n"
            "CRS codes for London terminals: "
            "PAD=Paddington, KGX=King's Cross, STP=St Pancras International, "
            "EUS=Euston, WAT=Waterloo, VIC=Victoria, LBG=London Bridge, "
            "CST=Cannon Street, CHX=Charing Cross, MYB=Marylebone, FST=Fenchurch Street.\n"
            "\n"
            "Example: Bristol → Cambridge via cross-London: "
            "from1=BRI, to1=PAD, from2=KGX, to2=CBG, transfer_mins=25"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from1":          {"type": "string",  "description": "Leg 1 origin CRS"},
                "to1":            {"type": "string",  "description": "Leg 1 destination / interchange CRS (the London terminal the train arrives at)"},
                "from2":          {"type": "string",  "description": "Leg 2 origin CRS (the London terminal the onward train departs from)"},
                "to2":            {"type": "string",  "description": "Leg 2 final destination CRS"},
                "transfer_mins":  {"type": "integer", "description": "Transfer time between the two terminals in minutes (default 25; see description for cross-London defaults)"},
                "date":           {"type": "string",  "description": "Date YYYY-MM-DD (default: today)"},
                "after":          {"type": "string",  "description": "After HHMM"},
                "arriveby":       {"type": "string",  "description": "Arriving at final destination by HHMM"},
            },
            "required": ["from1", "to1", "from2", "to2"],
        },
    },
]


@app.route("/mcp", methods=["GET"])
def mcp_get():
    # Some MCP clients probe with GET before sending POST requests.
    return jsonify({"name": "rtt", "version": "0.1.0", "protocol": "mcp"}), 200


@app.route("/mcp", methods=["POST"])
def mcp():
    body   = request.get_json(force=True) or {}
    method = body.get("method")
    msg_id = body.get("id")
    params = body.get("params") or {}

    def ok(result):
        return jsonify({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def err(code, msg):
        return jsonify({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": msg}})

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "rtt", "version": "0.1.0"},
            "instructions": (
                "This server provides live UK train information via the Real Time Trains API.\n"
                "\n"
                "CRITICAL — CROSS-LONDON ROUTING:\n"
                "Many UK journeys require crossing London between different terminals. "
                "Whenever a journey passes through London AND the origin/destination are on different "
                "sides of London (e.g. Bristol→Cambridge, Exeter→Norwich, Cardiff→Leeds, "
                "Southampton→Edinburgh), you MUST use search_route, not search_trains. "
                "search_route handles the cross-London transfer automatically using a transfer_mins "
                "window (default 25 min). Never call search_trains twice and try to join them manually.\n"
                "\n"
                "Common cross-London terminal pairs and transfer times:\n"
                "  PAD→KGX/STP: 25 min  |  PAD→EUS: 25 min  |  PAD→WAT: 30 min\n"
                "  PAD→VIC: 25 min      |  PAD→LBG: 30 min  |  KGX↔STP: 10 min\n"
                "  KGX↔EUS: 10 min      |  VIC↔WAT: 20 min  |  VIC↔LBG: 15 min\n"
                "\n"
                "London terminal CRS codes:\n"
                "  PAD=Paddington, KGX=King's Cross, STP=St Pancras, EUS=Euston,\n"
                "  WAT=Waterloo, VIC=Victoria, LBG=London Bridge, MYB=Marylebone,\n"
                "  CHX=Charing Cross, CST=Cannon Street, FST=Fenchurch Street\n"
                "\n"
                "PASSING POINTS:\n"
                "When asked where a train currently is, or for intermediate stations between stops, "
                "always call get_service_detail with include_passing=true."
            ),
        })

    if method in ("notifications/initialized",):
        return "", 204

    if method == "tools/list":
        return ok({"tools": _MCP_TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_trains":
                result = _search_trains(args["from_crs"], args["to_crs"],
                                        args.get("date"), args.get("after"), args.get("arriveby"))
            elif name == "get_service_detail":
                result = _get_service(args["identity"], args["departure_date"],
                                      args.get("from_crs"), args.get("to_crs"),
                                      include_passing=bool(args.get("include_passing", False)))
            elif name == "search_route":
                result = _search_route(args["from1"], args["to1"], args["from2"], args["to2"],
                                       int(args.get("transfer_mins", 25)),
                                       args.get("date"), args.get("after"), args.get("arriveby"))
            else:
                return err(-32601, f"Unknown tool: {name!r}")
        except KeyError as e:
            return err(-32602, f"Missing required argument: {e}")
        except Exception as e:
            return err(-32603, f"{type(e).__name__}: {e}")

        return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]})

    return err(-32601, f"Method not found: {method!r}")


# ── OpenAPI spec (used by Custom GPT action setup) ─────────────────────────────

_OPENAPI = {
    "openapi": "3.1.0",
    "info": {
        "title": "Real Time Trains",
        "description": (
            "Live UK train departure boards, service details, and multi-leg route connections.\n\n"
            "CROSS-LONDON ROUTING: Many UK journeys cross London between different terminals. "
            "Whenever a journey passes through London and the two rail legs use different terminals "
            "(e.g. Bristol→Cambridge, Exeter→Norwich, Cardiff→Leeds), use /api/route — NOT two "
            "separate calls to /api/trains. /api/route handles the cross-London transfer automatically "
            "via the transfer parameter (default 25 min). Never call /api/trains twice and join results manually.\n\n"
            "Common cross-London pairs and transfer times (minutes): "
            "PAD→KGX/STP 25, PAD→EUS 25, PAD→WAT 30, PAD→VIC 25, PAD→LBG 30, "
            "KGX↔STP 10, KGX↔EUS 10, VIC↔WAT 20, VIC↔LBG 15.\n\n"
            "London terminal CRS codes: PAD=Paddington, KGX=King's Cross, STP=St Pancras, "
            "EUS=Euston, WAT=Waterloo, VIC=Victoria, LBG=London Bridge, MYB=Marylebone, "
            "CHX=Charing Cross, CST=Cannon Street, FST=Fenchurch Street.\n\n"
            "PASSING POINTS: To see intermediate stations a train passes through without stopping, "
            "call /api/service with passing=true."
        ),
        "version": "0.1.0",
    },
    "servers": [{"url": "https://YOUR_API_GATEWAY_URL"}],
    "paths": {
        "/api/trains": {"get": {
            "operationId": "searchTrains",
            "summary": "Search for trains on a single rail leg (use searchRoute for cross-London journeys)",
            "parameters": [
                {"name": "from",     "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Origin CRS code (e.g. PAD)"},
                {"name": "to",       "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Destination CRS code"},
                {"name": "date",     "in": "query", "required": False, "schema": {"type": "string"}, "description": "Date YYYY-MM-DD"},
                {"name": "after",    "in": "query", "required": False, "schema": {"type": "string"}, "description": "Departing after HHMM"},
                {"name": "arriveby", "in": "query", "required": False, "schema": {"type": "string"}, "description": "Arriving by HHMM"},
            ],
            "responses": {"200": {"description": "Departure board"}},
        }},
        "/api/service": {"get": {
            "operationId": "getServiceDetail",
            "summary": "Get all calling points for a specific train service",
            "parameters": [
                {"name": "identity", "in": "query", "required": True,  "schema": {"type": "string"},  "description": "Service identity from searchTrains"},
                {"name": "date",     "in": "query", "required": True,  "schema": {"type": "string"},  "description": "Departure date YYYY-MM-DD"},
                {"name": "from",     "in": "query", "required": False, "schema": {"type": "string"},  "description": "Boarding station CRS"},
                {"name": "to",       "in": "query", "required": False, "schema": {"type": "string"},  "description": "Alighting station CRS"},
                {"name": "passing",  "in": "query", "required": False, "schema": {"type": "boolean"}, "description": "Include intermediate passing points"},
            ],
            "responses": {"200": {"description": "Calling points"}},
        }},
        "/api/route": {"get": {
            "operationId": "searchRoute",
            "summary": "Find connections for a two-leg journey with an interchange",
            "parameters": [
                {"name": "from1",    "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Leg 1 origin CRS"},
                {"name": "to1",      "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Leg 1 destination / interchange CRS"},
                {"name": "from2",    "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Leg 2 origin CRS"},
                {"name": "to2",      "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Leg 2 destination CRS"},
                {"name": "transfer", "in": "query", "required": False, "schema": {"type": "integer"}, "description": "Minimum transfer minutes (default: 25)"},
                {"name": "date",     "in": "query", "required": False, "schema": {"type": "string"}, "description": "Date YYYY-MM-DD"},
                {"name": "after",    "in": "query", "required": False, "schema": {"type": "string"}, "description": "After HHMM"},
                {"name": "arriveby", "in": "query", "required": False, "schema": {"type": "string"}, "description": "Arriving by HHMM"},
            ],
            "responses": {"200": {"description": "Connection list"}},
        }},
    },
}


@app.route("/openapi.json")
def openapi():
    return jsonify(_OPENAPI)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
