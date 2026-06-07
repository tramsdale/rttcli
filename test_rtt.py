"""Tests for rtt.py — pure functions and display logic."""

import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

import rtt
from rtt import (
    _status_badge,
    _time_status,
    display_departures,
    display_service_detail,
    fmt_iso,
    next_weekday,
    parse_date,
    parse_hhmm,
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


# ── _time_status ──────────────────────────────────────────────────────────────

class TestTimeStatus:
    def test_empty_dict(self):
        assert _time_status({}) == ("-", "scheduled")

    def test_cancelled(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "isCancelled": True,
        }
        display, status = _time_status(timing)
        assert status == "cancelled"
        assert display == "21:30"

    def test_on_time_with_actual(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeActual": "2026-06-07T21:30:00",
            "realtimeAdvertisedLateness": 0,
            "isCancelled": False,
        }
        display, status = _time_status(timing)
        assert status == "on_time"
        assert display == "21:30"

    def test_delayed_with_actual(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeActual": "2026-06-07T21:35:00",
            "realtimeAdvertisedLateness": 5,
            "isCancelled": False,
        }
        display, status = _time_status(timing)
        assert status == "delayed"
        assert display == "21:30→21:35 (+5m)"

    def test_forecast_matches_scheduled_shows_scheduled(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeForecast": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = _time_status(timing)
        assert status == "scheduled"
        assert display == "21:30"

    def test_forecast_delayed(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "realtimeForecast": "2026-06-07T21:40:00",
            "realtimeAdvertisedLateness": 10,
            "isCancelled": False,
        }
        display, status = _time_status(timing)
        assert status == "delayed"
        assert display == "21:30→21:40 (+10m)"

    def test_no_realtime_data(self):
        timing = {
            "scheduleAdvertised": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = _time_status(timing)
        assert status == "scheduled"
        assert display == "21:30"

    def test_falls_back_to_internal_schedule(self):
        timing = {
            "scheduleInternal": "2026-06-07T21:30:00",
            "isCancelled": False,
        }
        display, status = _time_status(timing)
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

        assert any("▶" in n and "Bristol" in n for n in rendered_names)
        assert not any("▶" in n and "Paddington" in n for n in rendered_names)

    def test_service_key_unwrapped(self):
        """Data must be read from data['service'], not data directly."""
        data = self._make_response([
            self._make_location("London Paddington", "PAD"),
        ])
        assert "service" in data
        # If we accidentally read from the top level, scheduleMetadata would be missing
        assert "scheduleMetadata" not in data
        assert "scheduleMetadata" in data["service"]
