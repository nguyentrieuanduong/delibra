from app.timefmt import utc_stamp, utc_stamp_range


def test_utc_stamp_formats_seconds_and_a_zero_offset() -> None:
    assert utc_stamp("2026-11-13T07:05:09.123456Z") == "2026-11-13 07:05:09 +00"
    assert utc_stamp("2026-11-13T07:05:09+02:00") == "2026-11-13 05:05:09 +00"


def test_utc_stamp_leaves_an_unreadable_value_alone() -> None:
    assert utc_stamp("yesterday") == "yesterday"
    assert utc_stamp(None) == ""


def test_a_same_day_range_names_the_day_and_offset_once() -> None:
    assert (
        utc_stamp_range("2026-11-13T07:05:09Z", "2026-11-13T07:12:30.5Z")
        == "2026-11-13 07:05:09 – 07:12:30 +00"
    )


def test_a_range_across_midnight_names_both_days() -> None:
    assert (
        utc_stamp_range("2026-11-13T23:59:01Z", "2026-11-14T00:03:40Z")
        == "2026-11-13 23:59:01 +00 – 2026-11-14 00:03:40 +00"
    )


def test_a_range_without_a_readable_finish_is_its_start() -> None:
    assert utc_stamp_range("2026-11-13T07:05:09Z", None) == "2026-11-13 07:05:09 +00"
    assert utc_stamp_range("2026-11-13T07:05:09Z", "later") == "2026-11-13 07:05:09 +00"
    assert utc_stamp_range("soon", "2026-11-13T07:05:09Z") == "soon"
