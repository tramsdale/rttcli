#!/usr/bin/env python3
"""
MCP server integration tests.

Usage:
    # Against deployed Lambda:
    uv run server/test_server.py

    # Against local dev server (run server/app.py first):
    uv run server/test_server.py --local

    # Specific base URL:
    uv run server/test_server.py --url https://myq9o4ul19.execute-api.eu-west-2.amazonaws.com/production
"""
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "rich"]
# ///

import argparse
import json
import sys
import time
from datetime import date, timedelta

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

DEFAULT_URL = "https://myq9o4ul19.execute-api.eu-west-2.amazonaws.com/production"
LOCAL_URL   = "http://localhost:5000"

console = Console()
_passed = _failed = 0


def mcp_call(base_url: str, method: str, params: dict = {}) -> dict:
    resp = requests.post(
        f"{base_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def rest_get(base_url: str, path: str, **params) -> dict:
    resp = requests.get(f"{base_url}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Test runner ────────────────────────────────────────────────────────────────

def check(label: str, passed: bool, detail: str = "") -> None:
    global _passed, _failed
    if passed:
        _passed += 1
        console.print(f"  [green]✓[/green] {label}")
    else:
        _failed += 1
        console.print(f"  [red]✗[/red] {label}")
    if detail:
        console.print(f"    [dim]{detail}[/dim]")


def section(title: str, pause: float = 15.0) -> None:
    if pause:
        console.print(f"  [dim]waiting {pause:.0f}s (rate limit)…[/dim]")
        time.sleep(pause)
    console.print(f"\n[bold blue]{title}[/bold blue]")


def dump(data: dict) -> None:
    console.print(Panel(
        json.dumps(data, indent=2, default=str)[:2000],
        box=box.MINIMAL, style="dim"
    ))


# ── Test suites ────────────────────────────────────────────────────────────────

def test_mcp_protocol(base_url: str) -> None:
    section("MCP protocol", pause=0)

    # GET probe
    try:
        r = requests.get(f"{base_url}/mcp", timeout=10)
        check("GET /mcp returns 200", r.status_code == 200)
    except Exception as e:
        check("GET /mcp returns 200", False, str(e))

    # initialize
    try:
        resp = mcp_call(base_url, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        result = resp.get("result", {})
        check("initialize returns protocolVersion", "protocolVersion" in result)
        check("initialize returns tools capability", "tools" in result.get("capabilities", {}))
    except Exception as e:
        check("initialize", False, str(e))

    # tools/list
    try:
        resp = mcp_call(base_url, "tools/list")
        tools = resp.get("result", {}).get("tools", [])
        names = {t["name"] for t in tools}
        check("tools/list returns 3 tools", len(tools) == 3, f"got {names}")
        for expected in ("search_trains", "get_service_detail", "search_route"):
            check(f"  tool '{expected}' present", expected in names)
    except Exception as e:
        check("tools/list", False, str(e))


def test_search_trains(base_url: str) -> None:
    section("search_trains tool")
    today = str(date.today())

    # Basic call
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_trains",
            "arguments": {"from_crs": "PAD", "to_crs": "BRI", "date": today},
        })
        result = resp.get("result", {})
        check("no error field", "error" not in resp)
        content = result.get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
        check("returns trains list", isinstance(data.get("trains"), list))
        check("trains list non-empty", len(data.get("trains", [])) > 0,
              f"got {len(data.get('trains', []))} trains")
        if data.get("trains"):
            t = data["trains"][0]
            check("train has departs field", "departs" in t)
            check("train has headcode",      "headcode" in t)
            check("train has identity",      bool(t.get("identity")))
            check("train has arrives field", "arrives" in t)
    except Exception as e:
        check("search_trains PAD→BRI", False, str(e))

    # arriveby filter
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_trains",
            "arguments": {"from_crs": "PAD", "to_crs": "BRI", "date": today, "arriveby": "1200"},
        })
        content = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
        trains = data.get("trains", [])
        check("arriveby filter returns trains", len(trains) > 0)
        # At most 2 trains should arrive after 12:00
        after = [t for t in trains if t.get("arrives") and t["arrives"] > "12:00"]
        check("arriveby: at most 2 trains after cutoff", len(after) <= 2,
              f"{len(after)} trains after 12:00")
    except Exception as e:
        check("search_trains arriveby", False, str(e))

    # Missing required arg (no RTT API call — no sleep needed)
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_trains",
            "arguments": {"from_crs": "PAD"},
        })
        check("missing to_crs returns error", "error" in resp)
    except Exception as e:
        check("missing arg error handling", False, str(e))


def test_get_service_detail(base_url: str) -> None:
    section("get_service_detail tool")
    today = str(date.today())

    # First get a train identity to use
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_trains",
            "arguments": {"from_crs": "PAD", "to_crs": "BRI", "date": today},
        })
        content = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        trains = json.loads(content).get("trains", [])
        if not trains:
            check("get_service_detail (no trains to test with)", False)
            return
        identity   = trains[0]["identity"]
        dep_date   = trains[0]["departure_date"]
    except Exception as e:
        check("setup: fetch identity for detail test", False, str(e))
        return

    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "get_service_detail",
            "arguments": {"identity": identity, "departure_date": dep_date,
                          "from_crs": "PAD", "to_crs": "BRI"},
        })
        err_msg = (resp.get("error") or {}).get("message", "")
        check("no error field", "error" not in resp, err_msg)
        content = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
        check("has calling_points",      isinstance(data.get("calling_points"), list))
        check("calling_points non-empty", len(data.get("calling_points", [])) > 0)
        check("has origin",              bool(data.get("origin")))
        check("has destination",         bool(data.get("destination")))
        # Check board/alight notes
        cps = data.get("calling_points", [])
        pad = [cp for cp in cps if cp.get("crs") == "PAD"]
        bri = [cp for cp in cps if cp.get("crs") == "BRI"]
        check("PAD has 'board here' note",  pad and pad[0].get("note") == "board here",
              f"PAD entry: {pad[0] if pad else 'not found'}")
        check("BRI has 'alight here' note", bri and bri[0].get("note") == "alight here",
              f"BRI entry: {bri[0] if bri else 'not found'}")
    except Exception as e:
        check("get_service_detail", False, str(e))


def test_search_route(base_url: str) -> None:
    section("search_route tool")
    today = str(date.today())
    tomorrow = str(date.today() + timedelta(days=1))

    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_route",
            "arguments": {
                "from1": "BRI", "to1": "PAD", "from2": "KGX", "to2": "CBG",
                "transfer_mins": 25, "date": tomorrow,
            },
        })
        err_msg = (resp.get("error") or {}).get("message", "")
        check("no error field", "error" not in resp, err_msg)
        content = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
        check("has connections list",      isinstance(data.get("connections"), list))
        check("connections non-empty",     len(data.get("connections", [])) > 0)
        if data.get("connections"):
            c = data["connections"][0]
            check("connection has leg1_departs",    "leg1_departs" in c)
            check("connection has leg2_departs",    "leg2_departs" in c)
            check("connection has interchange_arrives", "interchange_arrives" in c)
            check("connection has margin_mins",     "margin_mins" in c)
    except Exception as e:
        check("search_route BRI→PAD→KGX→CBG", False, str(e))

    # arriveby
    section("search_route tool — arriveby")
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_route",
            "arguments": {
                "from1": "BRI", "to1": "PAD", "from2": "KGX", "to2": "CBG",
                "transfer_mins": 25, "date": tomorrow, "arriveby": "1800",
            },
        })
        content = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
        conns = data.get("connections", [])
        check("arriveby: has connections", len(conns) > 0)
        after = [c for c in conns if c.get("final_arrives") and c["final_arrives"] > "18:00"]
        check("arriveby: at most 2 after cutoff", len(after) <= 2,
              f"{len(after)} connections after 18:00")
    except Exception as e:
        check("search_route arriveby", False, str(e))


def test_passing_points(base_url: str) -> None:
    section("get_service_detail — passing points")
    today = str(date.today())

    # Re-fetch a service identity
    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "search_trains",
            "arguments": {"from_crs": "PAD", "to_crs": "BRI", "date": today},
        })
        trains = json.loads(
            resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        ).get("trains", [])
        if not trains:
            check("passing points (no trains to test with)", False)
            return
        identity = trains[0]["identity"]
        dep_date = trains[0]["departure_date"]
    except Exception as e:
        check("setup: fetch identity for passing test", False, str(e))
        return

    try:
        resp = mcp_call(base_url, "tools/call", {
            "name": "get_service_detail",
            "arguments": {"identity": identity, "departure_date": dep_date,
                          "include_passing": True},
        })
        err_msg = (resp.get("error") or {}).get("message", "")
        check("no error with include_passing=true", "error" not in resp, err_msg)
        cps = json.loads(
            resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        ).get("calling_points", [])
        passes = [cp for cp in cps if cp.get("status") == "PASS"]
        check("passing points returned", len(passes) > 0,
              f"got {len(passes)} PASS entries out of {len(cps)} total")
        if passes:
            timed = [p for p in passes if p.get("departs") or p.get("arrives")]
            check("passing points have timing", len(timed) > 0,
                  f"{len(timed)}/{len(passes)} passes have timing — additional detail {'enabled' if timed else 'NOT enabled'}")
    except Exception as e:
        check("get_service_detail with include_passing", False, str(e))


def test_rest_endpoints(base_url: str, pause: float = 15.0) -> None:
    section("REST endpoints", pause=pause)
    today = str(date.today())

    for path, params, label in [
        ("/api/trains",  {"from": "PAD", "to": "BRI"},         "GET /api/trains"),
        ("/api/route",   {"from1": "BRI", "to1": "PAD",
                          "from2": "KGX", "to2": "CBG"},       "GET /api/route"),
        ("/openapi.json", {},                                   "GET /openapi.json"),
    ]:
        try:
            data = rest_get(base_url, path, **params)
            check(f"{label} returns 200", True)
            if path == "/openapi.json":
                check("openapi has paths", "paths" in data)
            elif path == "/api/trains":
                check("trains response has trains list", isinstance(data.get("trains"), list))
            elif path == "/api/route":
                check("route response has connections", isinstance(data.get("connections"), list))
        except Exception as e:
            check(label, False, str(e))

    # Missing params → 400
    try:
        r = requests.get(f"{base_url}/api/trains", timeout=10)
        check("GET /api/trains with no params → 400", r.status_code == 400)
    except Exception as e:
        check("missing params → 400", False, str(e))


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="RTT server integration tests")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--local", action="store_true", help=f"Test local server ({LOCAL_URL})")
    grp.add_argument("--url",   metavar="URL",        help="Custom base URL")
    parser.add_argument("--dump", action="store_true", help="Dump full response for first search_trains call")
    parser.add_argument("--dump-service", action="store_true", help="Dump raw service detail (all locations incl. PASS) for first PAD→BRI train")
    args = parser.parse_args()

    base_url = LOCAL_URL if args.local else (args.url or DEFAULT_URL)

    console.print(f"\n[bold]RTT server tests[/bold]  [dim]{base_url}[/dim]\n")

    if args.dump:
        section("Raw search_trains response (PAD→BRI today)", pause=0)
        try:
            resp = mcp_call(base_url, "tools/call", {
                "name": "search_trains",
                "arguments": {"from_crs": "PAD", "to_crs": "BRI"},
            })
            dump(resp)
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    if args.dump_service:
        section("Raw service detail (PAD→BRI, include_passing=true)", pause=0)
        try:
            # Get an identity first
            trains_resp = mcp_call(base_url, "tools/call", {
                "name": "search_trains",
                "arguments": {"from_crs": "PAD", "to_crs": "BRI"},
            })
            trains = json.loads(
                trains_resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
            ).get("trains", [])
            if trains:
                identity = trains[0]["identity"]
                dep_date = trains[0]["departure_date"]
                console.print(f"[dim]Service {identity} on {dep_date}[/dim]")
                # Call REST /api/service directly so we get the full raw dict
                svc_resp = rest_get(base_url, "/api/service",
                                    identity=identity, date=dep_date, passing="true")
                cps = svc_resp.get("calling_points", [])
                passes = [cp for cp in cps if cp.get("status") == "PASS"]
                console.print(f"[bold]{len(cps)} total calling points, {len(passes)} PASS entries[/bold]")
                if passes:
                    console.print("\n[bold]PASS entries:[/bold]")
                    for p in passes:
                        console.print(f"  {p}")
                else:
                    console.print("[yellow]No PASS entries in response — additional detail may not be active, or this service has no passing points.[/yellow]")
                    console.print("\n[dim]All calling points:[/dim]")
                    for cp in cps:
                        console.print(f"  {cp}")
        except Exception as e:
            console.print(f"[red]{e}[/red]")
        sys.exit(0)

    test_mcp_protocol(base_url)
    test_search_trains(base_url)
    test_get_service_detail(base_url)
    test_search_route(base_url)
    test_passing_points(base_url)
    test_rest_endpoints(base_url, pause=30)

    total = _passed + _failed
    colour = "green" if _failed == 0 else "red"
    console.print(f"\n[{colour}][bold]{_passed}/{total} passed[/bold][/{colour}]")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
