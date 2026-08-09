"""Tests for rtt.py — pure functions and display logic."""

import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import rtt
from rtt import (
    _status_badge,
    time_status,
    api_search_grouped,
    display_departures,
    display_service_detail,
    find_arrival_at,
    find_next_service_after,
    fmt_iso,
    get_terminus_arrival,
    next_weekday,
    parse_date,
    parse_hhmm,
    parse_iso_naive,
    resolve_arrivals,
    resolve_arrivals_grouped,
    resolve_station_group,
)


# ── fmt_iso ───────────────────────────────────────────────────────────────────

class TestFmtIso:
    def test_none_returns_empty(self):
        assert fmt_iso(None) == ""

    def test_empty_string_returns_empty(self):
        assert fmt_iso("") == ""

    def test_utc_timestamp(self):
        assert fmt_iso("2026-06-07T21:30:00Z") == "21:30"

    def test_no_timezone(self):
        assert fmt_iso("2026-06-07T09:05:00") == "09:05"

    def test_midnight(self):
        assert fmt_iso("2026-06-07T00:00:00") == "00:00"


# ── parse_date ────────────────────────────────────────────────────────────────

class TestParseDate:
    def test_dd_mm_yy(self):
        assert parse_date("7/6/26") == date(2026, 6, 7)

    def test_dd_mm_yy_zero_padded(self):
        assert parse_date("07/06/26") == date(2026, 6, 7)

    def test_dd_mm_yyyy(self):
        assert parse_date("07/06/2026") == date(2026, 6, 7)

    def test_iso_format(self):
        assert parse_date("2026-06-07") == date(2026, 6, 7)

    def test_invalid_exits(self):
        with patch.object(rtt.console, "print"), pytest.raises(SystemExit):
            parse_date("not-a-date")

    def test_wrong_separator_exits(self):
        with patch.object(rtt.console, "print"), pytest.raises(SystemExit):
            parse_date("2026.06.07")


# ── parse_hhmm ────────────────────────────────────────────────────────────────

class TestParseHhmm:
    def test_four_digits(self):
        assert parse_hhmm("2100") == (21, 0)

    def test_zero_padded_short(self):
        assert parse_hhmm("900") == (9, 0)

    def test_midnight(self):
        assert parse_hhmm("0000") == (0, 0)

    def test_last_minute_of_day(self):
        assert parse_hhmm("2359") == (23, 59)

    def test_invalid_hour_exits(self):
        with patch.object(rtt.console, "print"), pytest.raises(SystemExit):
            parse_hhmm("2400")

    def test_invalid_minute_exits(self):
        with patch.object(rtt.console, "print"), pytest.raises(SystemExit):
            parse_hhmm("2160")

    def test_letters_exit(self):
        with patch.object(rtt.console, "print"), pytest.raises(SystemExit):
            parse_hhmm("abcd")


# ── next_weekday ──────────────────────────────────────────────────────────────

class TestNextWeekday:
    def test_result_is_always_in_future(self):
        today = date.today()
        for weekday in range(7):
            result = next_weekday(weekday)
            assert result > today

    def test_result_has_correct_weekday(self):
        for weekday in range(7):
            result = next_weekday(weekday)
            assert result.weekday() == weekday

    def test_same_weekday_as_today_goes_to_next_week(self):
        today = date.today()
        result = next_weekday(today.weekday())
        assert result == today + timedelta(days=7)

    def test_next_day_is_one_day_ahead(self):
        today = date.today()
        tomorrow_weekday = (today.weekday() + 1) % 7
        result = next_weekday(tomorrow_weekday)
        assert result == today + timedelta(days=1)


# ── time_status ──────────────────────────────────────────────────────────────

class TestTimeStatus:
    def test_empty_dict(self):
        assert time_status({}) == ("-", "scheduled")

    def test_cancelled(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "isCancelled": True,
        }
        display, status = time_status(timing)
        assert status == "cancelled"
        assert display == "21:30"

    def test_on_time_with_actual(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeActual": "2026-06-07T21:30:00",
            "realtimeAdvertisedLateness": 0,
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert status == "on_time"
        assert display == "21:30"

    def test_delayed_with_actual(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeActual": "2026-06-07T21:35:00",
            "realtimeAdvertisedLateness": 5,
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert status == "delayed"
        assert display == "21:30→21:35 (+5m)"

    def test_forecast_matches_scheduled_shows_scheduled(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeForecast": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert status == "scheduled"
        assert display == "21:30"

    def test_forecast_delayed(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeForecast": "2026-06-07T21:40:00",
            "realtimeAdvertisedLateness": 10,
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert status == "delayed"
        assert display == "21:30→21:40 (+10m)"

    def test_no_realtime_data(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert status == "scheduled"
        assert display == "21:30"

    def test_falls_back_to_internal_schedule(self):
        timing = {
            "scheduleInternal": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = time_status(timing)
        assert display == "21:30"


# ── _status_badge ─────────────────────────────────────────────────────────────

class TestStatusBadge:
    def _badge_text(self, display_as, temporal=None, reasons=None):
        return _status_badge(display_as, temporal or {}, reasons or []).plain

    def test_cancelled(self):
        assert self._badge_text("CANCELLED") == "Cancelled"

    def test_diverted(self):
        assert self._badge_text("DIVERTED") == "Diverted"

    def test_starts(self):
        assert self._badge_text("STARTS") == "Starts here"

    def test_terminates(self):
        assert self._badge_text("TERMINATES") == "Terminates"

    def test_delayed_no_reason(self):
        temporal = {"departure": {"realtimeAdvertisedLateness": 8}}
        text = self._badge_text("CALL", temporal)
        assert text == "Delayed +8m"

    def test_delayed_with_reason(self):
        temporal = {"departure": {"realtimeAdvertisedLateness": 5}}
        reasons = [{"shortText": "Awaiting staff"}]
        text = self._badge_text("CALL", temporal, reasons)
        assert "Delayed +5m" in text
        assert "Awaiting staff" in text

    def test_on_time_with_actual(self):
        temporal = {"departure": {"realtimeActual": "2026-06-07T21:30:00"}}
        assert self._badge_text("CALL", temporal) == "On time"

    def test_scheduled_no_realtime(self):
        assert self._badge_text("CALL", {}) == "Scheduled"


# ── parse_iso_naive ───────────────────────────────────────────────────────────

class TestParseIsoNaive:
    def test_basic(self):
        assert parse_iso_naive("2026-06-07T21:30:00") == datetime(2026, 6, 7, 21, 30, 0)

    def test_strips_timezone(self):
        assert parse_iso_naive("2026-06-07T21:30:00+01:00") == datetime(2026, 6, 7, 21, 30, 0)

    def test_strips_z(self):
        assert parse_iso_naive("2026-06-07T09:05:00Z") == datetime(2026, 6, 7, 9, 5, 0)

    def test_none_returns_none(self):
        assert parse_iso_naive(None) is None

    def test_empty_returns_none(self):
        assert parse_iso_naive("") is None

    def test_invalid_returns_none(self):
        assert parse_iso_naive("not-a-date") is None


# ── get_terminus_arrival ──────────────────────────────────────────────────────

class TestGetTerminusArrival:
    def _make_svc(self, dest_codes, arrival_time):
        return {
            "destination": [{
                "location": {"shortCodes": dest_codes, "longCodes": []},
                "temporalData": {"scheduleAdvertised": arrival_time},
            }]
        }

    def test_matches_short_code(self):
        svc = self._make_svc(["PAD"], "2026-06-07T09:05:00")
        result = get_terminus_arrival(svc, "PAD")
        assert result == datetime(2026, 6, 7, 9, 5, 0)

    def test_case_insensitive(self):
        svc = self._make_svc(["PAD"], "2026-06-07T09:05:00")
        assert get_terminus_arrival(svc, "pad") is not None

    def test_no_match_returns_none(self):
        svc = self._make_svc(["KGX"], "2026-06-07T09:05:00")
        assert get_terminus_arrival(svc, "PAD") is None

    def test_empty_destination_returns_none(self):
        assert get_terminus_arrival({"destination": []}, "PAD") is None


# ── find_arrival_at ───────────────────────────────────────────────────────────

class TestFindArrivalAt:
    def _make_detail(self, stops):
        """stops: list of (crs, arrival_time_str)"""
        return {
            "service": {
                "locations": [
                    {
                        "location": {"shortCodes": [crs]},
                        "temporalData": {
                            "arrival": {"scheduleAdvertised": arr_time},
                        },
                    }
                    for crs, arr_time in stops
                ]
            }
        }

    def test_finds_station(self):
        data = self._make_detail([("PAD", "2026-06-07T09:05:00"), ("BRI", "2026-06-07T10:00:00")])
        assert find_arrival_at(data, "PAD") == datetime(2026, 6, 7, 9, 5, 0)

    def test_finds_intermediate_stop(self):
        data = self._make_detail([
            ("BRI", "2026-06-07T07:30:00"),
            ("RDG", "2026-06-07T08:45:00"),
            ("PAD", "2026-06-07T09:05:00"),
        ])
        assert find_arrival_at(data, "RDG") == datetime(2026, 6, 7, 8, 45, 0)

    def test_missing_station_returns_none(self):
        data = self._make_detail([("PAD", "2026-06-07T09:05:00")])
        assert find_arrival_at(data, "CBG") is None

    def test_handles_top_level_data_without_service_key(self):
        data = {
            "locations": [{
                "location": {"shortCodes": ["PAD"]},
                "temporalData": {"arrival": {"scheduleAdvertised": "2026-06-07T09:05:00"}},
            }]
        }
        assert find_arrival_at(data, "PAD") == datetime(2026, 6, 7, 9, 5, 0)


# ── find_next_service_after ───────────────────────────────────────────────────

class TestFindNextServiceAfter:
    def _make_services(self, dep_times):
        return [
            {
                "temporalData": {
                    "departure": {"scheduleAdvertised": f"2026-06-07T{t}:00"}
                }
            }
            for t in dep_times
        ]

    def test_returns_first_at_or_after(self):
        svcs = self._make_services(["08:00", "08:30", "09:00"])
        earliest = datetime(2026, 6, 7, 8, 30, 0)
        result = find_next_service_after(svcs, earliest)
        dep = result["temporalData"]["departure"]["scheduleAdvertised"]
        assert "08:30" in dep

    def test_returns_exact_match(self):
        svcs = self._make_services(["09:00", "09:30"])
        result = find_next_service_after(svcs, datetime(2026, 6, 7, 9, 0, 0))
        assert "09:00" in result["temporalData"]["departure"]["scheduleAdvertised"]

    def test_returns_none_when_all_too_early(self):
        svcs = self._make_services(["07:00", "07:30"])
        assert find_next_service_after(svcs, datetime(2026, 6, 7, 10, 0, 0)) is None

    def test_empty_list_returns_none(self):
        assert find_next_service_after([], datetime(2026, 6, 7, 9, 0, 0)) is None

    def test_skips_earlier_services(self):
        svcs = self._make_services(["08:00", "09:00", "10:00"])
        result = find_next_service_after(svcs, datetime(2026, 6, 7, 9, 30, 0))
        assert "10:00" in result["temporalData"]["departure"]["scheduleAdvertised"]


# ── resolve_arrivals ──────────────────────────────────────────────────────────

class TestResolveArrivals:
    def _make_svc_with_terminus(self, dest_crs, arr_time):
        return {
            "destination": [{
                "location": {"shortCodes": [dest_crs], "longCodes": []},
                "temporalData": {"scheduleAdvertised": arr_time},
            }],
            "scheduleMetadata": {"identity": "X001", "departureDate": "2026-06-07"},
        }

    def test_uses_terminus_when_available(self):
        svcs = [self._make_svc_with_terminus("PAD", "2026-06-07T09:05:00")]
        result = resolve_arrivals(svcs, "PAD")
        assert result == [datetime(2026, 6, 7, 9, 5, 0)]

    def test_returns_none_when_no_match_and_no_identity(self):
        svcs = [{"destination": [], "scheduleMetadata": {}}]
        result = resolve_arrivals(svcs, "PAD")
        assert result == [None]

    def test_length_matches_services(self):
        svcs = [
            self._make_svc_with_terminus("PAD", "2026-06-07T09:05:00"),
            self._make_svc_with_terminus("PAD", "2026-06-07T09:35:00"),
        ]
        result = resolve_arrivals(svcs, "PAD")
        assert len(result) == 2

    def test_mixed_terminus_and_none(self):
        svcs = [
            self._make_svc_with_terminus("PAD", "2026-06-07T09:05:00"),
            {"destination": [], "scheduleMetadata": {}},  # no terminus match, no identity
        ]
        result = resolve_arrivals(svcs, "PAD")
        assert result[0] == datetime(2026, 6, 7, 9, 5, 0)
        assert result[1] is None


# ── resolve_station_group ─────────────────────────────────────────────────────

class TestResolveStationGroup:
    def test_known_alias_expands(self):
        assert resolve_station_group("LON") == ["KGX", "STP"]

    def test_lowercase_alias_expands(self):
        assert resolve_station_group("lon") == ["KGX", "STP"]

    def test_unknown_code_returns_itself(self):
        assert resolve_station_group("PAD") == ["PAD"]

    def test_unknown_code_is_uppercased(self):
        assert resolve_station_group("pad") == ["PAD"]


# ── resolve_arrivals_grouped ──────────────────────────────────────────────────

class TestResolveArrivalsGrouped:
    def _make_svc_with_terminus(self, dest_crs, arr_time, grp_to=None):
        svc = {
            "destination": [{
                "location": {"shortCodes": [dest_crs], "longCodes": []},
                "temporalData": {"scheduleAdvertised": arr_time},
            }],
            "scheduleMetadata": {"identity": "X001", "departureDate": "2026-06-07"},
        }
        if grp_to is not None:
            svc["_grp_to"] = grp_to
        return svc

    def test_uses_grp_to_tag_over_passed_to_crs(self):
        """A grouped search tags each service with the real station it matched."""
        svc = self._make_svc_with_terminus("STP", "2026-06-07T09:05:00", grp_to="STP")
        result = resolve_arrivals_grouped([svc], "LON")
        assert result == [datetime(2026, 6, 7, 9, 5, 0)]

    def test_falls_back_to_to_crs_when_untagged(self):
        svc = self._make_svc_with_terminus("PAD", "2026-06-07T09:05:00")
        result = resolve_arrivals_grouped([svc], "PAD")
        assert result == [datetime(2026, 6, 7, 9, 5, 0)]


# ── api_search_grouped ────────────────────────────────────────────────────────

class TestApiSearchGrouped:
    def _api_response(self, from_desc, from_crs, to_desc, to_crs, dep_times):
        return {
            "query": {
                "location": {"description": from_desc},
                "filterTo": {"description": to_desc},
            },
            "services": [
                {
                    "temporalData": {"departure": {"scheduleAdvertised": f"2026-06-07T{t}:00"}},
                    "scheduleMetadata": {"identity": f"X{t.replace(':', '')}", "departureDate": "2026-06-07"},
                }
                for t in dep_times
            ],
        }

    def test_ungrouped_behaves_like_single_search(self):
        with patch("rtt.api_search") as mock_search:
            mock_search.return_value = self._api_response(
                "London Paddington", "PAD", "Bristol Temple Meads", "BRI", ["09:00"]
            )
            services, meta = api_search_grouped("PAD", "BRI", "2026-06-07T08:00:00")
        mock_search.assert_called_once()
        assert len(services) == 1
        assert services[0]["_grp_to"] is None
        assert services[0]["_grp_from"] is None
        assert meta["from_label"] == "London Paddington"
        assert meta["to_label"] == "Bristol Temple Meads"

    def test_grouped_destination_merges_and_tags(self):
        def fake_search(f, t, time_from, time_window=240):
            if t == "KGX":
                return self._api_response("Cambridge", "CBG", "London Kings Cross", "KGX", ["06:00", "07:00"])
            return self._api_response("Cambridge", "CBG", "St Pancras International", "STP", ["06:30"])

        with patch("rtt.api_search", side_effect=fake_search):
            services, meta = api_search_grouped("CBG", "LON", "2026-06-07T05:00:00")

        assert len(services) == 3
        assert meta["to_group"] == ["KGX", "STP"]
        assert "Kings Cross" in meta["to_label"] and "St Pancras" in meta["to_label"]
        assert [s["_grp_to"] for s in services] == ["KGX", "STP", "KGX"]

    def test_grouped_results_sorted_by_departure(self):
        def fake_search(f, t, time_from, time_window=240):
            if t == "KGX":
                return self._api_response("Cambridge", "CBG", "London Kings Cross", "KGX", ["07:00"])
            return self._api_response("Cambridge", "CBG", "St Pancras International", "STP", ["06:00"])

        with patch("rtt.api_search", side_effect=fake_search):
            services, _ = api_search_grouped("CBG", "LON", "2026-06-07T05:00:00")

        deps = [s["temporalData"]["departure"]["scheduleAdvertised"] for s in services]
        assert deps == sorted(deps)

    def test_no_services_returns_empty_list(self):
        with patch("rtt.api_search", return_value=None):
            services, meta = api_search_grouped("CBG", "LON", "2026-06-07T05:00:00")
        assert services == []


# ── display_departures ────────────────────────────────────────────────────────

class TestDisplayDepartures:
    """Test data extraction logic, with console output suppressed."""

    def _make_service(self, dep_time, dest_name, headcode="1A00", platform="5", operator="Test Rail"):
        return {
            "temporalData": {
                "departure": {
                    "scheduleAdvertised": f"2026-06-07T{dep_time}:00",
                    "isCancelled": False,
                },
                "displayAs": "CALL",
            },
            "locationMetadata": {"platform": {"planned": platform}},
            "scheduleMetadata": {
                "identity": "X12345",
                "departureDate": "2026-06-07",
                "trainReportingIdentity": headcode,
                "operator": {"name": operator},
            },
            "destination": [
                {"location": {"description": dest_name}}
            ],
            "reasons": [],
        }

    def test_returns_services_list(self):
        data = {
            "query": {
                "location": {"description": "London Paddington"},
                "filterTo": {"description": "Bristol Temple Meads"},
                "timeFrom": "2026-06-07T21:00:00",
            },
            "services": [self._make_service("21:30", "Bristol Temple Meads")],
        }
        with patch.object(rtt.console, "print"):
            result = display_departures(data, "PAD", "BRI")
        assert len(result) == 1

    def test_empty_services_returns_empty_list(self):
        data = {"query": {}, "services": []}
        with patch.object(rtt.console, "print"):
            result = display_departures(data, "PAD", "BRI")
        assert result == []

    def test_destination_extracted_from_nested_location(self):
        """destination[0].location.description, not destination[0].description"""
        svc = self._make_service("21:30", "Bristol Temple Meads")
        # Confirm the fixture nests it correctly
        assert svc["destination"][0].get("description") is None
        assert svc["destination"][0]["location"]["description"] == "Bristol Temple Meads"

    def test_header_shows_expanded_names_and_codes(self):
        data = {
            "query": {
                "location": {"description": "London Kings Cross"},
                "filterTo": {"description": "Cambridge"},
                "timeFrom": "2026-06-07T21:00:00",
            },
            "services": [],
        }
        printed = []
        with patch.object(rtt.console, "print", side_effect=lambda *a, **k: printed.append(str(a))):
            display_departures(data, "KGX", "CBG")
        header = next(s for s in printed if "Kings Cross" in s)
        assert "London Kings Cross (KGX)" in header
        assert "Cambridge (CBG)" in header

    def test_header_falls_back_to_crs_when_no_query(self):
        data = {"query": {}, "services": []}
        printed = []
        with patch.object(rtt.console, "print", side_effect=lambda *a, **k: printed.append(str(a))):
            display_departures(data, "PAD", "BRI")
        header = next(s for s in printed if "PAD" in s)
        assert "(PAD)" in header
        assert "(BRI)" in header

    def test_multiple_services_all_returned(self):
        data = {
            "query": {},
            "services": [
                self._make_service("21:30", "Bristol Temple Meads"),
                self._make_service("22:00", "Bristol Parkway"),
                self._make_service("22:30", "Weston-super-Mare"),
            ],
        }
        with patch.object(rtt.console, "print"):
            result = display_departures(data, "PAD", "BRI")
        assert len(result) == 3

    def test_group_meta_uses_combined_label_in_header(self):
        data = {"query": {}, "services": []}
        group_meta = {
            "from_label": "Cambridge",
            "to_label": "London Kings Cross / St Pancras International",
            "from_group": ["CBG"],
            "to_group": ["KGX", "STP"],
        }
        printed = []
        with patch.object(rtt.console, "print", side_effect=lambda *a, **k: printed.append(str(a))):
            display_departures(data, "CBG", "LON", group_meta=group_meta)
        header = next(s for s in printed if "Cambridge" in s)
        assert "London Kings Cross / St Pancras International (LON)" in header

    def test_group_meta_annotates_arrival_with_real_station(self):
        """A grouped destination search adds e.g. '(KGX)' after each arrival time."""
        svc = self._make_service("06:00", "London Kings Cross")
        svc["_grp_to"] = "KGX"
        data = {"query": {}, "services": [svc]}
        group_meta = {
            "from_label": "Cambridge", "to_label": "London Kings Cross / St Pancras International",
            "from_group": ["CBG"], "to_group": ["KGX", "STP"],
        }
        arrivals = [datetime(2026, 6, 7, 7, 0, 0)]

        rendered_rows = []
        real_table_class = rtt.Table

        class CapturingTable(real_table_class):
            def add_row(self, *args, **kwargs):
                rendered_rows.append([str(a) for a in args])
                super().add_row(*args, **kwargs)

        with patch.object(rtt.console, "print"), patch("rtt.Table", CapturingTable):
            display_departures(data, "CBG", "LON", arrivals=arrivals, group_meta=group_meta)

        assert rendered_rows[0][4] == "07:00 (KGX)"

    def test_group_meta_annotates_departure_with_real_station(self):
        """A grouped origin search adds e.g. '(KGX)' after each departure time."""
        svc = self._make_service("06:00", "Bristol Temple Meads")
        svc["_grp_from"] = "KGX"
        data = {"query": {}, "services": [svc]}
        group_meta = {
            "from_label": "London Kings Cross / St Pancras International", "to_label": "Bristol Temple Meads",
            "from_group": ["KGX", "STP"], "to_group": ["BRI"],
        }

        rendered_rows = []
        real_table_class = rtt.Table

        class CapturingTable(real_table_class):
            def add_row(self, *args, **kwargs):
                rendered_rows.append([str(a) for a in args])
                super().add_row(*args, **kwargs)

        with patch.object(rtt.console, "print"), patch("rtt.Table", CapturingTable):
            display_departures(data, "LON", "BRI", group_meta=group_meta)

        assert rendered_rows[0][1] == "06:00 (KGX)"

    def test_no_group_meta_omits_annotation(self):
        """Ungrouped searches render exactly as before, with no '(CRS)' suffix."""
        svc = self._make_service("06:00", "Bristol Temple Meads")
        data = {"query": {}, "services": [svc]}
        arrivals = [datetime(2026, 6, 7, 7, 0, 0)]

        rendered_rows = []
        real_table_class = rtt.Table

        class CapturingTable(real_table_class):
            def add_row(self, *args, **kwargs):
                rendered_rows.append([str(a) for a in args])
                super().add_row(*args, **kwargs)

        with patch.object(rtt.console, "print"), patch("rtt.Table", CapturingTable):
            display_departures(data, "PAD", "BRI", arrivals=arrivals)

        assert rendered_rows[0][1] == "06:00"
        assert rendered_rows[0][4] == "07:00"


# ── display_service_detail ────────────────────────────────────────────────────

class TestDisplayServiceDetail:
    """Test that the 'service' wrapper key is handled and PASS stops are skipped."""

    def _make_response(self, locations):
        return {
            "systemStatus": {"rttCore": "OK"},
            "service": {
                "scheduleMetadata": {
                    "identity": "W34739",
                    "trainReportingIdentity": "1C32",
                    "departureDate": "2026-06-07",
                    "operator": {"name": "Great Western Railway"},
                    "modeType": "TRAIN",
                },
                "origin": [{"location": {"description": "London Paddington"}}],
                "destination": [{"location": {"description": "Bristol Temple Meads"}}],
                "locations": locations,
            },
        }

    def _make_location(self, name, crs, display_as="CALL", dep_time="21:30"):
        return {
            "temporalData": {
                "departure": {
                    "scheduleAdvertised": f"2026-06-07T{dep_time}:00",
                    "isCancelled": False,
                },
                "displayAs": display_as,
            },
            "locationMetadata": {"platform": {"planned": "5"}},
            "location": {
                "description": name,
                "shortCodes": [crs],
            },
            "reasons": [],
            "associatedServices": [],
        }

    def test_renders_without_error(self):
        data = self._make_response([
            self._make_location("London Paddington", "PAD"),
            self._make_location("Bristol Temple Meads", "BRI", dep_time="23:02"),
        ])
        with patch.object(rtt.console, "print"):
            display_service_detail(data)  # should not raise

    def test_pass_stops_are_skipped(self):
        """PASS locations should not appear in the table rows."""
        locations = [
            self._make_location("London Paddington", "PAD", display_as="CALL"),
            self._make_location("Didcot Parkway", "DID", display_as="PASS"),
            self._make_location("Bristol Temple Meads", "BRI", display_as="CALL"),
        ]
        data = self._make_response(locations)

        rendered_names = []
        original_add_row = None

        # Capture what gets added to the table
        real_table_class = rtt.Table

        class CapturingTable(real_table_class):
            def add_row(self, *args, **kwargs):
                # First arg is the location name Text object
                if args:
                    rendered_names.append(str(args[0]))
                super().add_row(*args, **kwargs)

        with patch.object(rtt.console, "print"), patch("rtt.Table", CapturingTable):
            display_service_detail(data)

        assert any("Paddington" in n for n in rendered_names)
        assert any("Bristol" in n for n in rendered_names)
        assert not any("Didcot" in n for n in rendered_names)

    def test_highlight_crs_adds_arrow(self):
        """The destination CRS station name should be prefixed with ▶."""
        locations = [
            self._make_location("London Paddington", "PAD", display_as="CALL"),
            self._make_location("Bristol Temple Meads", "BRI", display_as="CALL"),
        ]
        data = self._make_response(locations)

        rendered_names = []

        real_table_class = rtt.Table

        class CapturingTable(real_table_class):
            def add_row(self, *args, **kwargs):
                if args:
                    rendered_names.append(str(args[0]))
                super().add_row(*args, **kwargs)

        with patch.object(rtt.console, "print"), patch("rtt.Table", CapturingTable):
            display_service_detail(data, highlight_crs="BRI")

        assert any("↓" in n and "Bristol" in n for n in rendered_names)
        assert not any("↓" in n and "Paddington" in n for n in rendered_names)

    def test_service_key_unwrapped(self):
        """Data must be read from data['service'], not data directly."""
        data = self._make_response([
            self._make_location("London Paddington", "PAD"),
        ])
        assert "service" in data
        # If we accidentally read from the top level, scheduleMetadata would be missing
        assert "scheduleMetadata" not in data
        assert "scheduleMetadata" in data["service"]
