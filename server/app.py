"""
RTT MCP + REST server — deployable to AWS Lambda via Zappa.

MCP endpoint:  POST /mcp   (Claude / any MCP client)
REST endpoints: GET /api/trains, /api/service, /api/route  (Custom GPT / any HTTP client)
OpenAPI spec:  GET /openapi.json  (paste URL into Custom GPT action setup)

Environment variables:
    RTT_TOKEN   — your Real Time Trains API bearer token
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

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
    r.raise_for_status()
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

    try:
        data = rtt.api_search(from_crs, to_crs, time_from)
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


def _get_service(identity: str, dep_date: str, from_crs: str | None, to_crs: str | None) -> dict:
    try:
        data = rtt.api_service(identity, dep_date)
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
        if display_as == "PASS":
            continue
        if display_as is None and not is_advertised:
            continue

        location = loc.get("location", {})
        codes = location.get("shortCodes") or []
        arr = temporal.get("arrival") or {}
        dep = temporal.get("departure") or {}

        def _t(timing: dict) -> str | None:
            s = (timing.get("realtimeActual") or timing.get("realtimeForecast") or
                 timing.get("scheduleAdvertised") or "")[11:16]
            return s or None

        plat_obj = ((loc.get("locationMetadata") or {}).get("platform") or {})
        platform = plat_obj.get("actual") or plat_obj.get("forecast") or plat_obj.get("planned") or "-"

        point: dict = {
            "station":  location.get("description", "-"),
            "crs":      codes[0] if codes else "-",
            "arrives":  _t(arr),
            "departs":  _t(dep),
            "platform": platform,
            "status":   display_as or "CALL",
        }
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
        s2 = rtt.find_next_service_after(leg2_svcs, conn["leg2_earliest"])
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
                "leg2_departs":       dep2,
                "leg2_headcode":      m2.get("trainReportingIdentity", "-"),
                "final_arrives":      conn["leg2_arr"].strftime("%H:%M") if conn.get("leg2_arr") else None,
                "margin_mins":        margin,
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
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except SystemExit:
        return jsonify({"error": "RTT API error"}), 502


@app.route("/api/service")
def rest_service():
    try:
        identity = request.args.get("identity", "").strip()
        dep_date = request.args.get("date", "").strip()
        if not identity or not dep_date:
            return jsonify({"error": "identity and date query parameters are required"}), 400
        return jsonify(_get_service(identity, dep_date,
                                    from_crs=request.args.get("from"),
                                    to_crs=request.args.get("to")))
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except SystemExit:
        return jsonify({"error": "RTT API error"}), 502


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
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    except SystemExit:
        return jsonify({"error": "RTT API error"}), 502


# ── MCP endpoint (Streamable HTTP, protocol version 2024-11-05) ────────────────

_MCP_TOOLS = [
    {
        "name": "search_trains",
        "description": (
            "Search for UK train services between two stations. Returns departure times, "
            "arrival times at the destination, headcodes, platforms, operators, and live status."
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
            "departure_date from a search_trains result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identity":       {"type": "string", "description": "Service identity (e.g. W34739)"},
                "departure_date": {"type": "string", "description": "Date YYYY-MM-DD"},
                "from_crs":       {"type": "string", "description": "Boarding station CRS (optional)"},
                "to_crs":         {"type": "string", "description": "Alighting station CRS (optional)"},
            },
            "required": ["identity", "departure_date"],
        },
    },
    {
        "name": "search_route",
        "description": (
            "Find connections for a two-leg journey with an interchange (e.g. train then tube). "
            "Returns matched connections with transfer margins and final arrival times."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from1":          {"type": "string", "description": "Leg 1 origin CRS"},
                "to1":            {"type": "string", "description": "Leg 1 destination / interchange CRS"},
                "from2":          {"type": "string", "description": "Leg 2 origin CRS"},
                "to2":            {"type": "string", "description": "Leg 2 destination CRS"},
                "transfer_mins":  {"type": "integer", "description": "Minimum transfer time in minutes (default: 25)"},
                "date":           {"type": "string", "description": "Date YYYY-MM-DD (default: today)"},
                "after":          {"type": "string", "description": "After HHMM"},
                "arriveby":       {"type": "string", "description": "Arriving at final destination by HHMM"},
            },
            "required": ["from1", "to1", "from2", "to2"],
        },
    },
]


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
                                      args.get("from_crs"), args.get("to_crs"))
            elif name == "search_route":
                result = _search_route(args["from1"], args["to1"], args["from2"], args["to2"],
                                       int(args.get("transfer_mins", 25)),
                                       args.get("date"), args.get("after"), args.get("arriveby"))
            else:
                return err(-32601, f"Unknown tool: {name!r}")
        except KeyError as e:
            return err(-32602, f"Missing required argument: {e}")
        except (ValueError, RuntimeError) as e:
            return err(-32603, f"Tool error: {e}")
        except SystemExit as e:
            return err(-32603, f"RTT API error: {e}")

        return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]})

    return err(-32601, f"Method not found: {method!r}")


# ── OpenAPI spec (used by Custom GPT action setup) ─────────────────────────────

_OPENAPI = {
    "openapi": "3.1.0",
    "info": {
        "title": "Real Time Trains",
        "description": "Live UK train departure boards, service details, and multi-leg route connections.",
        "version": "0.1.0",
    },
    "servers": [{"url": "https://YOUR_API_GATEWAY_URL"}],
    "paths": {
        "/api/trains": {"get": {
            "operationId": "searchTrains",
            "summary": "Search for trains between two UK stations",
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
                {"name": "identity", "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Service identity from searchTrains"},
                {"name": "date",     "in": "query", "required": True,  "schema": {"type": "string"}, "description": "Departure date YYYY-MM-DD"},
                {"name": "from",     "in": "query", "required": False, "schema": {"type": "string"}, "description": "Boarding station CRS"},
                {"name": "to",       "in": "query", "required": False, "schema": {"type": "string"}, "description": "Alighting station CRS"},
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
