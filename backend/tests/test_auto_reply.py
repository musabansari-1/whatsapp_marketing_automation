from datetime import UTC, datetime, timedelta

from app.domain.auto_reply import looks_like_auto_reply

NOW = datetime(2026, 11, 7, 15, 40, tzinfo=UTC)
GREETING = "Welcome to TRY FOR WALK\n\nHow can we help you today?"


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def test_first_and_second_greeting_are_answered() -> None:
    assert not looks_like_auto_reply(GREETING, [], now=NOW)
    assert not looks_like_auto_reply(GREETING, [(ago(1), GREETING)], now=NOW)


def test_third_identical_message_in_half_an_hour_is_a_loop() -> None:
    earlier = [(ago(1), GREETING), (ago(2), GREETING)]
    assert looks_like_auto_reply(GREETING, earlier, now=NOW)


def test_whitespace_and_case_do_not_hide_a_repeat() -> None:
    earlier = [(ago(1), "welcome to try for walk how can we help you today?"), (ago(2), GREETING)]
    assert looks_like_auto_reply(GREETING, earlier, now=NOW)


def test_old_repeats_do_not_count() -> None:
    earlier = [(ago(31), GREETING), (ago(45), GREETING)]
    assert not looks_like_auto_reply(GREETING, earlier, now=NOW)


def test_a_person_writing_different_things_is_not_a_loop() -> None:
    earlier = [(ago(1), "Dr. Mehta"), (ago(2), "Physiotherapy"), (ago(3), "Lucknow")]
    assert not looks_like_auto_reply("Evening after 6", earlier, now=NOW)


def test_eight_messages_in_ten_minutes_is_a_loop() -> None:
    earlier = [(ago(i), f"text {i}") for i in range(1, 8)]
    assert looks_like_auto_reply("text 0", earlier, now=NOW)


def test_seven_messages_in_ten_minutes_is_not() -> None:
    earlier = [(ago(i), f"text {i}") for i in range(1, 7)]
    assert not looks_like_auto_reply("text 0", earlier, now=NOW)


def test_empty_bodies_never_count_as_repeats() -> None:
    earlier = [(ago(1), None), (ago(2), "")]
    assert not looks_like_auto_reply("", earlier, now=NOW)
