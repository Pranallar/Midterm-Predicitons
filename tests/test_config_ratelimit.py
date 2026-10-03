"""Tests for config.py, ratelimit.py, errors.py and display.py.

Everything here is offline and uses fake time; the thread tests use real threads but
a shared fake clock, so nothing actually sleeps for long.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import List

import pytest

from conftest import API_KEY, FakeClock, Sleeper

from supermarket_bot.config import (
    DEFAULT_BASE_URL,
    DEFAULT_READS_PER_MIN,
    DEFAULT_WRITES_PER_MIN,
    ConfigError,
    Settings,
    load_env_file,
    mask_key,
    parse_env_file,
)
from supermarket_bot.display import fmt_delta, fmt_num, fmt_price, sparkline, table, truncate
from supermarket_bot.errors import AUTH_CODES, HINTS, ApiError, NetworkError, SuperMarketError
from supermarket_bot.ratelimit import SlidingWindowLimiter

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "supermarket-openapi.json"
EMAIL_MESSAGE = "Confirm the email address on this API key's account to use the API."
TICKS = "▁▂▃▄▅▆▇█"


def spec_error_codes() -> dict:
    """``{code: status}`` from the error table in the spec's info.description."""
    desc = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["info"]["description"]
    return {code: int(status) for code, status in re.findall(r"^\|\s*`([A-Z_]+)`\s*\|\s*(\d{3})\s*\|", desc, re.M)}


# =========================================================================== config: parse_env_file


class TestParseEnvFile:
    def test_basic_pairs_and_whitespace(self):
        text = "A=1\n  B = two  \nC=three"
        assert parse_env_file(text) == {"A": "1", "B": "two", "C": "three"}

    def test_comments_and_blank_lines_ignored(self):
        text = "\n# a comment\n   # indented comment\n\n\t\nKEY=value\n#OTHER=hidden\n"
        assert parse_env_file(text) == {"KEY": "value"}

    def test_export_prefix(self):
        text = "export A=1\nexport    B=2\n  export C = 3"
        assert parse_env_file(text) == {"A": "1", "B": "2", "C": "3"}

    def test_export_like_key_name_is_not_stripped(self):
        # Only "export " followed by whitespace is the shell keyword.
        assert parse_env_file("exported=1") == {"exported": "1"}

    def test_double_and_single_quotes_stripped(self):
        text = "A=\"quoted value\"\nB='single quoted'\nC=\"  padded  \""
        assert parse_env_file(text) == {"A": "quoted value", "B": "single quoted", "C": "  padded  "}

    def test_quoted_value_keeps_hash(self):
        text = "A=\"has #hash inside\"\nB='x # y'"
        assert parse_env_file(text) == {"A": "has #hash inside", "B": "x # y"}

    def test_mismatched_or_lone_quotes_left_alone(self):
        text = "A=\"abc'\nB=\"\nC=it's"
        assert parse_env_file(text) == {"A": "\"abc'", "B": '"', "C": "it's"}

    def test_empty_quotes_give_empty_string(self):
        assert parse_env_file("A=\"\"\nB=''") == {"A": "", "B": ""}

    def test_inline_comment_stripped_from_unquoted_value(self):
        text = "A=value # trailing comment\nB=value    #  spaced"
        assert parse_env_file(text) == {"A": "value", "B": "value"}

    def test_hash_without_preceding_space_is_part_of_value(self):
        text = "URL=https://example.test/page#frag\nPASS=abc#def"
        assert parse_env_file(text) == {"URL": "https://example.test/page#frag", "PASS": "abc#def"}

    def test_lines_without_equals_are_skipped(self):
        text = "JUSTAWORD\nexport NOEQ\nA=1"
        assert parse_env_file(text) == {"A": "1"}

    def test_empty_key_skipped_and_empty_value_kept(self):
        assert parse_env_file("=orphan\nEMPTY=\n  = also orphan") == {"EMPTY": ""}

    def test_value_may_contain_equals(self):
        assert parse_env_file("A=b=c==") == {"A": "b=c=="}

    def test_later_duplicate_wins(self):
        assert parse_env_file("A=1\nA=2") == {"A": "2"}

    def test_crlf_line_endings(self):
        assert parse_env_file("A=1\r\nB='2'\r\n") == {"A": "1", "B": "2"}

    def test_quoted_value_followed_by_inline_comment(self):
        # Both bash and python-dotenv read this as  ace_secret_123  (no quotes).
        text = 'SUPERMARKET_API_KEY="ace_secret_123"  # my key\nB=\'x\' # note'
        assert parse_env_file(text) == {"SUPERMARKET_API_KEY": "ace_secret_123", "B": "x"}

    def test_load_env_file_missing_file_returns_empty(self, tmp_path):
        assert load_env_file(tmp_path / "does-not-exist.env") == {}

    def test_load_env_file_reads_utf8(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("NAME='Café → ok'\n", encoding="utf-8")
        assert load_env_file(p) == {"NAME": "Café → ok"}


# =========================================================================== config: Settings.load

LONG_KEY = "ace_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def write_env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


class TestSettingsLoad:
    def test_defaults_with_only_key(self, tmp_path):
        s = Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY}, env_file=tmp_path / "missing")
        assert s.api_key == LONG_KEY
        assert s.base_url == DEFAULT_BASE_URL
        assert s.tournament is None
        assert s.reads_per_min == DEFAULT_READS_PER_MIN == 90
        assert s.writes_per_min == DEFAULT_WRITES_PER_MIN == 25
        assert s.data_dir == Path("data")

    def test_defaults_stay_under_documented_standard_budget(self):
        # Spec: standard accounts get 100 reads and 30 writes per minute.
        assert 1 <= DEFAULT_READS_PER_MIN <= 100
        assert 1 <= DEFAULT_WRITES_PER_MIN <= 30

    def test_reads_everything_from_env_file(self, tmp_path):
        env_file = write_env(
            tmp_path,
            "# settings\n"
            f"export SUPERMARKET_API_KEY='{LONG_KEY}'\n"
            "SUPERMARKET_BASE_URL=https://staging.example.test/api/v1/\n"
            "SUPERMARKET_TOURNAMENT=predictions-cup  # slug\n"
            "SUPERMARKET_READS_PER_MIN=40\n"
            "SUPERMARKET_WRITES_PER_MIN=\"10\"\n"
            "SUPERMARKET_DATA_DIR=/tmp/sm-data\n",
        )
        s = Settings.load(env={}, env_file=env_file)
        assert s.api_key == LONG_KEY
        assert s.base_url == "https://staging.example.test/api/v1"
        assert s.tournament == "predictions-cup"
        assert s.reads_per_min == 40
        assert s.writes_per_min == 10
        assert s.data_dir == Path("/tmp/sm-data")

    def test_environment_beats_env_file(self, tmp_path):
        env_file = write_env(
            tmp_path,
            "SUPERMARKET_API_KEY=file_key_0123456789\nSUPERMARKET_TOURNAMENT=from-file\nSUPERMARKET_READS_PER_MIN=11\n",
        )
        s = Settings.load(
            env={"SUPERMARKET_API_KEY": "env_key_0123456789", "SUPERMARKET_TOURNAMENT": "from-env"},
            env_file=env_file,
        )
        assert s.api_key == "env_key_0123456789"
        assert s.tournament == "from-env"
        assert s.reads_per_min == 11  # not in env, so the file value still applies

    def test_overrides_beat_environment_and_env_file(self, tmp_path):
        env_file = write_env(tmp_path, "SUPERMARKET_API_KEY=file_key_0123456789\nSUPERMARKET_DATA_DIR=file-dir\n")
        s = Settings.load(
            env={"SUPERMARKET_API_KEY": "env_key_0123456789", "SUPERMARKET_TOURNAMENT": "from-env",
                 "SUPERMARKET_READS_PER_MIN": "50", "SUPERMARKET_BASE_URL": "https://env.test/api/v1"},
            env_file=env_file,
            api_key="override_key_0123456789",
            tournament="from-override",
            reads_per_min=7,
            writes_per_min=3,
            data_dir="override-dir",
            base_url="https://override.test/api/v1/",
        )
        assert s.api_key == "override_key_0123456789"
        assert s.tournament == "from-override"
        assert s.reads_per_min == 7
        assert s.writes_per_min == 3
        assert s.data_dir == Path("override-dir")
        assert s.base_url == "https://override.test/api/v1"

    def test_none_override_falls_back_to_environment(self, tmp_path):
        s = Settings.load(
            env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_TOURNAMENT": "from-env"},
            env_file=tmp_path / "missing",
            tournament=None,
            api_key=None,
        )
        assert s.api_key == LONG_KEY
        assert s.tournament == "from-env"

    def test_env_none_reads_os_environ(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUPERMARKET_API_KEY", LONG_KEY)
        monkeypatch.setenv("SUPERMARKET_READS_PER_MIN", "33")
        s = Settings.load(env_file=tmp_path / "missing")
        assert s.api_key == LONG_KEY
        assert s.reads_per_min == 33

    def test_env_file_none_ignores_dotenv(self, tmp_path, monkeypatch):
        write_env(tmp_path, f"SUPERMARKET_API_KEY={LONG_KEY}\n")
        monkeypatch.chdir(tmp_path)  # so the default Path(".env") would find it
        with pytest.raises(ConfigError):
            Settings.load(env={}, env_file=None)
        # ...while the default env_file does pick it up from the cwd.
        assert Settings.load(env={}).api_key == LONG_KEY

    @pytest.mark.parametrize("value", [None, "", "   ", "\t"])
    def test_missing_or_blank_key_raises(self, tmp_path, value):
        env = {} if value is None else {"SUPERMARKET_API_KEY": value}
        with pytest.raises(ConfigError, match="SUPERMARKET_API_KEY"):
            Settings.load(env=env, env_file=tmp_path / "missing")

    def test_config_error_is_runtime_error(self):
        assert issubclass(ConfigError, RuntimeError)

    def test_values_are_stripped(self, tmp_path):
        s = Settings.load(
            env={"SUPERMARKET_API_KEY": f"  {LONG_KEY}\n", "SUPERMARKET_TOURNAMENT": "  cup  ",
                 "SUPERMARKET_READS_PER_MIN": " 12 "},
            env_file=tmp_path / "missing",
        )
        assert s.api_key == LONG_KEY
        assert s.tournament == "cup"
        assert s.reads_per_min == 12

    def test_blank_optional_values_fall_back_to_defaults(self, tmp_path):
        s = Settings.load(
            env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_TOURNAMENT": "  ", "SUPERMARKET_BASE_URL": "",
                 "SUPERMARKET_READS_PER_MIN": "", "SUPERMARKET_WRITES_PER_MIN": "   ", "SUPERMARKET_DATA_DIR": ""},
            env_file=tmp_path / "missing",
        )
        assert s.tournament is None
        assert s.base_url == DEFAULT_BASE_URL
        assert (s.reads_per_min, s.writes_per_min) == (DEFAULT_READS_PER_MIN, DEFAULT_WRITES_PER_MIN)
        assert s.data_dir == Path("data")

    @pytest.mark.parametrize("name", ["SUPERMARKET_READS_PER_MIN", "SUPERMARKET_WRITES_PER_MIN"])
    @pytest.mark.parametrize("bad", ["abc", "1.5", "0", "-3", "ten", "5 per min"])
    def test_invalid_rate_integers_raise(self, tmp_path, name, bad):
        with pytest.raises(ConfigError) as info:
            Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY, name: bad}, env_file=tmp_path / "missing")
        assert name in str(info.value)
        assert LONG_KEY not in str(info.value)

    def test_zero_reads_error_message(self, tmp_path):
        with pytest.raises(ConfigError, match=r"at least 1, got 0"):
            Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_READS_PER_MIN": "0"},
                          env_file=tmp_path / "missing")

    def test_non_integer_error_message_quotes_value(self, tmp_path):
        with pytest.raises(ConfigError, match=r"must be an integer, got 'abc'"):
            Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_READS_PER_MIN": "abc"},
                          env_file=tmp_path / "missing")

    def test_invalid_integer_in_env_file_raises(self, tmp_path):
        env_file = write_env(tmp_path, f"SUPERMARKET_API_KEY={LONG_KEY}\nSUPERMARKET_WRITES_PER_MIN=lots\n")
        with pytest.raises(ConfigError, match="SUPERMARKET_WRITES_PER_MIN"):
            Settings.load(env={}, env_file=env_file)

    def test_valid_env_value_masks_invalid_file_value(self, tmp_path):
        env_file = write_env(tmp_path, f"SUPERMARKET_API_KEY={LONG_KEY}\nSUPERMARKET_READS_PER_MIN=junk\n")
        s = Settings.load(env={"SUPERMARKET_READS_PER_MIN": "60"}, env_file=env_file)
        assert s.reads_per_min == 60

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://x.test/api/v1/", "https://x.test/api/v1"),
            ("https://x.test/api/v1///", "https://x.test/api/v1"),
            ("https://x.test/api/v1", "https://x.test/api/v1"),
            ("  https://x.test/api/v1/  ", "https://x.test/api/v1"),
        ],
    )
    def test_base_url_trailing_slash_stripped(self, tmp_path, raw, expected):
        s = Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_BASE_URL": raw},
                          env_file=tmp_path / "missing")
        assert s.base_url == expected

    def test_settings_are_frozen(self, tmp_path):
        s = Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY}, env_file=tmp_path / "missing")
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.api_key = "other"  # type: ignore[misc]


class TestSecretHandling:
    def test_repr_and_str_never_contain_full_key(self, tmp_path):
        s = Settings.load(env={"SUPERMARKET_API_KEY": LONG_KEY, "SUPERMARKET_TOURNAMENT": "cup"},
                          env_file=tmp_path / "missing")
        for text in (repr(s), str(s), f"{s}", "%s" % (s,), repr([s]), str({"s": s})):
            assert LONG_KEY not in text
        assert mask_key(LONG_KEY) in repr(s)
        assert "base_url='https://www.thesuper.market/api/v1'" in repr(s)
        assert "tournament='cup'" in repr(s)
        assert str(s) == repr(s)

    def test_repr_with_short_key_shows_only_stars(self):
        s = Settings(api_key="short")
        assert "short" not in repr(s)
        assert "api_key='***'" in repr(s)

    def test_mask_key_long(self):
        assert mask_key(API_KEY) == f"{API_KEY[:6]}…{API_KEY[-4:]}"
        assert mask_key(LONG_KEY) == "ace_li…6789"

    @pytest.mark.parametrize("key", ["", "a", "0123456789", "ace_short"])
    def test_mask_key_short_keys_fully_hidden(self, key):
        assert mask_key(key) == "***"

    def test_mask_key_never_reveals_full_key_for_any_length(self):
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP"
        for n in range(1, len(alphabet) + 1):
            key = alphabet[:n]
            masked = mask_key(key)
            assert key not in masked
            assert len(masked) <= 11


# =========================================================================== ratelimit


@pytest.fixture
def fclock() -> FakeClock:
    return FakeClock(start=1000.0)


@pytest.fixture
def fsleep(fclock: FakeClock) -> Sleeper:
    return Sleeper(fclock)


def limiter(limit: int, fclock: FakeClock, fsleep: Sleeper, window: float = 60.0) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(limit, window=window, clock=fclock, sleep=fsleep)


class TestSlidingWindowLimiter:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_limit_must_be_positive(self, bad):
        with pytest.raises(ValueError):
            SlidingWindowLimiter(bad)

    def test_requests_up_to_limit_do_not_wait(self, fclock, fsleep):
        lim = limiter(5, fclock, fsleep)
        assert [lim.acquire() for _ in range(5)] == [0.0] * 5
        assert fsleep.calls == []
        assert lim.used == 5

    def test_request_over_limit_waits_until_oldest_leaves_window(self, fclock, fsleep):
        lim = limiter(3, fclock, fsleep)
        for _ in range(3):
            lim.acquire()
            fclock.now += 10  # stamps at 1000, 1010, 1020; now 1030
        waited = lim.acquire()
        assert waited == pytest.approx(30.0)  # until 1000 + 60
        assert fclock.now == pytest.approx(1060.0)
        # The next slot opens when the 1010 stamp ages out.
        assert lim.acquire() == pytest.approx(10.0)
        assert fclock.now == pytest.approx(1070.0)
        assert fsleep.calls == pytest.approx([30.0, 10.0])

    def test_never_more_than_limit_in_any_window(self, fclock, fsleep):
        lim = limiter(4, fclock, fsleep)
        times: List[float] = []
        for _ in range(17):
            lim.acquire()
            times.append(fclock.now)
            fclock.now += 1.5
        for i in range(len(times) - 4):
            assert times[i + 4] - times[i] >= 60.0 - 1e-9

    def test_window_slides(self, fclock, fsleep):
        lim = limiter(2, fclock, fsleep)
        lim.acquire()
        fclock.now += 30
        lim.acquire()
        assert lim.used == 2
        fclock.now += 30  # first stamp exactly 60s old -> out of the window
        assert lim.used == 1
        assert lim.acquire() == 0.0
        fclock.now += 29.5
        assert lim.used == 2
        fclock.now += 0.5
        assert lim.used == 1
        fclock.now += 60
        assert lim.used == 0
        assert fsleep.calls == []

    def test_just_inside_window_still_counts(self, fclock, fsleep):
        lim = limiter(1, fclock, fsleep)
        lim.acquire()
        fclock.now += 59.75
        assert lim.used == 1
        assert lim.acquire() == pytest.approx(0.25)

    def test_used_does_not_consume_budget(self, fclock, fsleep):
        lim = limiter(2, fclock, fsleep)
        for _ in range(10):
            assert lim.used == 0
        lim.acquire()
        lim.acquire()
        assert fsleep.calls == []

    def test_custom_window(self, fclock, fsleep):
        lim = limiter(2, fclock, fsleep, window=1.0)
        lim.acquire()
        lim.acquire()
        assert lim.acquire() == pytest.approx(1.0)  # stamps now at t, t, t+1 -> only t+1 left
        assert lim.used == 1
        fclock.now += 0.5
        assert lim.used == 1
        fclock.now += 0.5
        assert lim.used == 0

    def test_pause_blocks_even_with_free_budget(self, fclock, fsleep):
        lim = limiter(100, fclock, fsleep)
        lim.pause(60)
        assert lim.acquire() == pytest.approx(60.0)
        assert fclock.now == pytest.approx(1060.0)
        # Pause is over: the following requests go straight through.
        assert lim.acquire() == 0.0
        assert lim.used == 2

    def test_pause_counts_from_when_it_was_called(self, fclock, fsleep):
        lim = limiter(100, fclock, fsleep)
        lim.pause(30)
        fclock.now += 20
        assert lim.acquire() == pytest.approx(10.0)

    def test_expired_pause_has_no_effect(self, fclock, fsleep):
        lim = limiter(100, fclock, fsleep)
        lim.pause(5)
        fclock.now += 5
        assert lim.acquire() == 0.0
        assert fsleep.calls == []

    def test_pause_never_shortens_existing_pause(self, fclock, fsleep):
        lim = limiter(100, fclock, fsleep)
        lim.pause(60)
        lim.pause(10)
        lim.pause(0)
        assert lim.acquire() == pytest.approx(60.0)

    def test_later_longer_pause_extends(self, fclock, fsleep):
        lim = limiter(100, fclock, fsleep)
        lim.pause(10)
        fclock.now += 5
        lim.pause(30)  # until 1035, later than 1010
        assert lim.acquire() == pytest.approx(30.0)
        assert fclock.now == pytest.approx(1035.0)

    @pytest.mark.parametrize("seconds", [-5, -0.1, 0])
    def test_non_positive_pause_is_noop(self, fclock, fsleep, seconds):
        lim = limiter(100, fclock, fsleep)
        lim.pause(seconds)
        assert lim.acquire() == 0.0
        assert fsleep.calls == []

    def test_pause_and_full_window_wait_for_the_later_of_the_two(self, fclock, fsleep):
        lim = limiter(1, fclock, fsleep)
        lim.acquire()  # window frees at 1060
        lim.pause(10)  # pause ends at 1010
        assert lim.acquire() == pytest.approx(60.0)
        lim.pause(100)  # now 1060, pause ends 1160; window would free at 1120
        assert lim.acquire() == pytest.approx(100.0)

    def test_pause_does_not_reset_window(self, fclock, fsleep):
        lim = limiter(2, fclock, fsleep)
        lim.acquire()
        lim.acquire()
        lim.pause(1)
        fclock.now += 1
        assert lim.used == 2
        assert lim.acquire() == pytest.approx(59.0)

    def test_pause_during_a_wait_extends_that_wait(self, fclock):
        """A 429 seen by another caller while we sleep must also hold us."""
        state = {"lim": None, "paused": False}

        def sleep(seconds: float) -> None:
            if not state["paused"]:
                state["paused"] = True
                state["lim"].pause(100)  # at t=1000 -> pause until 1100
            fclock.now += seconds

        lim = SlidingWindowLimiter(1, clock=fclock, sleep=sleep)
        state["lim"] = lim
        lim.acquire()
        waited = lim.acquire()  # window wait 60, then the pause adds 40
        assert waited == pytest.approx(100.0)
        assert fclock.now == pytest.approx(1100.0)

    def test_short_sleeps_are_retried_until_a_slot_is_free(self, fclock):
        calls: List[float] = []

        def sleep(seconds: float) -> None:
            calls.append(seconds)
            fclock.now += min(seconds, 25.0)  # wakes up early

        lim = SlidingWindowLimiter(1, clock=fclock, sleep=sleep)
        lim.acquire()
        lim.acquire()
        assert fclock.now >= 1060.0
        assert calls == pytest.approx([60.0, 35.0, 10.0])

    def test_lock_not_held_while_sleeping(self, fclock):
        """``used`` and ``pause`` from inside sleep() would deadlock if acquire held the lock."""
        seen: List[int] = []
        holder = {}

        def sleep(seconds: float) -> None:
            seen.append(holder["lim"].used)
            holder["lim"].pause(0)
            fclock.now += seconds

        lim = SlidingWindowLimiter(1, clock=fclock, sleep=sleep)
        holder["lim"] = lim
        lim.acquire()
        worker = threading.Thread(target=lim.acquire, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "acquire() deadlocked: lock held across sleep()"
        assert seen == [1]

    def test_pause_blocks_every_waiting_thread(self, fclock):
        """Two callers arrive during a pause: both must wait it out."""
        clock_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)
        waited = {}

        def sleep(seconds: float) -> None:
            barrier.wait()  # both threads are now blocked by the pause
            with clock_lock:
                fclock.now = max(fclock.now, 1000.0 + seconds)

        lim = SlidingWindowLimiter(100, clock=fclock, sleep=sleep)
        lim.pause(30)

        def run(name: str) -> None:
            waited[name] = lim.acquire()

        threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not any(t.is_alive() for t in threads)
        assert waited == {"a": pytest.approx(30.0), "b": pytest.approx(30.0)}
        assert lim.used == 2

    def test_thread_safety_smoke(self):
        """Many threads, one shared fake clock: the window invariant must hold."""
        limit, n_threads, per_thread = 5, 12, 4
        clock = FakeClock(start=0.0)
        clock_lock = threading.Lock()

        def sleep(seconds: float) -> None:
            with clock_lock:
                clock.now += seconds
            time.sleep(0)  # let other threads run

        lim = SlidingWindowLimiter(limit, clock=clock, sleep=sleep)
        appended: List[float] = []
        overfull: List[int] = []

        class RecordingDeque(deque):
            def append(self, item):  # called under the limiter's lock
                if len(self) >= limit:
                    overfull.append(len(self))
                appended.append(item)
                super().append(item)

        lim._stamps = RecordingDeque()
        errors: List[BaseException] = []

        def work() -> None:
            try:
                for _ in range(per_thread):
                    lim.acquire()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=work, daemon=True) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)
        assert errors == []
        assert overfull == []
        assert len(appended) == n_threads * per_thread
        stamps = sorted(appended)
        for i in range(len(stamps) - limit):
            assert stamps[i + limit] - stamps[i] >= 60.0 - 1e-6


# =========================================================================== errors


class TestApiError:
    def test_str_with_method_and_path(self):
        err = ApiError(404, "NOT_FOUND", "Market not found", method="GET", path="/markets/26")
        assert str(err) == "HTTP 404 NOT_FOUND: Market not found (GET /markets/26)"
        assert err.args == (str(err),)

    def test_str_without_path_has_no_location(self):
        err = ApiError(400, "VALIDATION_ERROR", "bad ids", method="GET")
        assert str(err) == "HTTP 400 VALIDATION_ERROR: bad ids"

    def test_attributes_and_defaults(self):
        err = ApiError(429, "RATE_LIMITED", "slow down", retry_after=60.0)
        assert (err.status, err.code, err.message, err.retry_after) == (429, "RATE_LIMITED", "slow down", 60.0)
        assert err.details == {}
        assert (err.method, err.path) == ("", "")
        details = {"required": ["read"], "missing": ["read"]}
        assert ApiError(403, "INSUFFICIENT_SCOPES", "x", details=details).details == details

    def test_hierarchy(self):
        err = ApiError(500, "INTERNAL_ERROR", "boom")
        assert isinstance(err, SuperMarketError) and isinstance(err, Exception)
        assert issubclass(NetworkError, SuperMarketError)
        assert not issubclass(NetworkError, ApiError)
        with pytest.raises(SuperMarketError, match="INTERNAL_ERROR"):
            raise err

    def test_hint_appended_to_str(self):
        err = ApiError(401, "API_KEY_REVOKED", "Key revoked", method="GET", path="/me")
        assert err.hint == HINTS["API_KEY_REVOKED"]
        assert str(err) == f"HTTP 401 API_KEY_REVOKED: Key revoked (GET /me) — {HINTS['API_KEY_REVOKED']}"

    @pytest.mark.parametrize("code", sorted(HINTS))
    def test_every_hinted_code_has_hint(self, code):
        err = ApiError(400, code, "msg")
        assert err.hint == HINTS[code]
        assert str(err).endswith(" — " + HINTS[code])

    def test_unknown_code_has_no_hint(self):
        err = ApiError(418, "TEAPOT", "short and stout")
        assert err.hint is None
        assert "—" not in str(err)

    def test_forbidden_email_confirmation_hint(self):
        # Spec: unconfirmed email -> 403 FORBIDDEN with exactly this message.
        err = ApiError(403, "FORBIDDEN", EMAIL_MESSAGE, method="GET", path="/me")
        assert err.hint is not None
        assert "Confirm the email" in err.hint
        assert str(err).startswith(f"HTTP 403 FORBIDDEN: {EMAIL_MESSAGE} (GET /me) — ")
        assert str(err).endswith(err.hint)

    @pytest.mark.parametrize("message", ["Not a member of this tournament", "Forbidden", ""])
    def test_generic_forbidden_has_no_hint(self, message):
        err = ApiError(403, "FORBIDDEN", message)
        assert err.hint is None
        assert "—" not in str(err)

    def test_email_message_under_other_code_gets_that_codes_hint(self):
        err = ApiError(403, "ACCOUNT_BANNED", EMAIL_MESSAGE)
        assert err.hint == HINTS["ACCOUNT_BANNED"]

    def test_auth_codes_are_documented_401_or_403(self):
        codes = spec_error_codes()
        assert codes, "could not parse the error table from the spec"
        for code in AUTH_CODES:
            assert code in codes, code
            assert codes[code] in (401, 403), code

    def test_retryable_and_upstream_codes_are_not_auth_codes(self):
        # UNAUTHORIZED is documented as "not a client-key problem"; the others are transient.
        for code in ("UNAUTHORIZED", "RATE_LIMITED", "TX_CONFLICT", "SERVICE_UNAVAILABLE", "INTERNAL_ERROR", "NOT_FOUND"):
            assert code not in AUTH_CODES

    def test_hints_only_for_documented_codes(self):
        codes = spec_error_codes()
        assert set(HINTS) <= set(codes)
        assert HINTS["INSUFFICIENT_SCOPES"].count("read")  # market data needs the read scope


# =========================================================================== display


class TestFormatters:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, "—"), (True, "—"), (False, "—"), ("0.5", "—"), ([0.5], "—"),
            (0, "0.000"), (1, "1.000"), (0.5, "0.500"), (0.12345, "0.123"), (0.4567, "0.457"),
            (-0.25, "-0.250"), (0.9999, "1.000"),
        ],
    )
    def test_fmt_price(self, value, expected):
        assert fmt_price(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, "—"), (True, "—"), (False, "—"), ("12", "—"),
            (0, "0"), (0.0, "0"), (7, "7"), (1234, "1,234"), (1234.0, "1,234"), (-1234567.0, "-1,234,567"),
            (1234.567, "1,234.57"), (0.5, "0.50"), (-0.125, "-0.12"), (10**9, "1,000,000,000"),
        ],
    )
    def test_fmt_num(self, value, expected):
        assert fmt_num(value) == expected

    def test_fmt_num_digits(self):
        assert fmt_num(3.14159, digits=4) == "3.1416"
        assert fmt_num(1234.5, digits=1) == "1,234.5"
        assert fmt_num(2.0, digits=5) == "2"  # integral floats never get decimals

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, ""), (True, ""), (False, ""), ("0.1", ""),
            (0, "+0.000"), (0.0, "+0.000"), (1, "+1.000"), (0.05, "+0.050"), (-0.1, "-0.100"), (0.12345, "+0.123"),
        ],
    )
    def test_fmt_delta(self, value, expected):
        assert fmt_delta(value) == expected

    @pytest.mark.parametrize(
        "text, width, expected",
        [
            (None, 5, ""), ("", 5, ""), ("abc", 5, "abc"), ("abcde", 5, "abcde"),
            ("abcdef", 5, "abcd…"), ("hello world", 2, "h…"), (123456, 4, "123…"),
        ],
    )
    def test_truncate(self, text, width, expected):
        out = truncate(text, width)
        assert out == expected
        assert len(out) <= width

    def test_truncate_width_one_respects_width(self):
        assert len(truncate("hello", 1)) <= 1


class TestTable:
    COLS = [("name", "Name"), ("px", "Price"), ("note", "Note")]

    def test_alignment_and_header_separator(self):
        rows = [{"name": "Alpha", "px": "0.410", "note": "x"}, {"name": "B", "px": "1", "note": "longer note"}]
        out = table(rows, self.COLS)
        assert out.splitlines() == [
            "Name   Price  Note",
            "-----  -----  -----------",
            "Alpha  0.410  x",
            "B      1      longer note",
        ]

    def test_columns_follow_spec_order_and_missing_values_blank(self):
        rows = [{"note": "n", "name": "a", "extra": "ignored"}, {"name": "b", "px": None}]
        lines = table(rows, self.COLS).splitlines()
        assert lines[0] == "Name  Price  Note"
        assert lines[2] == "a            n"
        assert lines[3] == "b"  # blank cells, trailing spaces stripped
        assert "ignored" not in "\n".join(lines)

    def test_numbers_are_stringified(self):
        out = table([{"name": 3, "px": 0.5, "note": 1234}], self.COLS)
        assert out.splitlines()[2] == "3     0.5    1234"

    def test_no_trailing_whitespace(self):
        rows = [{"name": "long name here", "px": "", "note": ""}, {"name": "x", "px": "y", "note": ""}]
        for line in table(rows, self.COLS).splitlines():
            assert line == line.rstrip()

    def test_empty_rows_render_header_only(self):
        assert table([], self.COLS) == "Name  Price  Note\n----  -----  ----"

    def test_accepts_generator_rows(self):
        rows = ({"name": str(i), "px": i, "note": ""} for i in range(3))
        assert len(table(rows, self.COLS).splitlines()) == 5

    def test_max_width_truncates_cells_and_caps_column(self):
        rows = [{"name": "A very long market title indeed", "px": "0.5", "note": "short"}]
        lines = table(rows, self.COLS, max_width={"name": 10}).splitlines()
        assert lines[2].startswith("A very lo…  0.5")
        assert lines[1].split("  ")[0] == "-" * 10
        # Other columns keep their natural width.
        assert lines[2].endswith("short")

    def test_max_width_keeps_header_visible(self):
        rows = [{"name": "abcdefgh", "px": "1", "note": ""}]
        lines = table(rows, [("name", "Market name"), ("px", "Px")], max_width={"name": 4}).splitlines()
        assert lines[0] == "Market name  Px"
        assert lines[2] == "abc…         1"

    def test_max_width_for_unknown_column_is_ignored(self):
        rows = [{"name": "abc", "px": "1", "note": ""}]
        assert table(rows, self.COLS, max_width={"nope": 1}) == table(rows, self.COLS)

    def test_columns_align_across_rows(self):
        rows = [{"name": "a" * n, "px": "p" * (5 - n), "note": "z"} for n in range(1, 5)]
        lines = table(rows, self.COLS).splitlines()
        starts = {line.index("z") for line in lines[2:]}
        assert len(starts) == 1


class TestSparkline:
    def test_empty_and_all_none(self):
        assert sparkline([]) == ""
        assert sparkline([None, None]) == ""

    def test_full_range_of_ticks(self):
        assert sparkline([0, 1, 2, 3, 4, 5, 6, 7]) == TICKS
        assert sparkline([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]) == TICKS

    def test_min_and_max_map_to_extremes(self):
        out = sparkline([0.42, 0.10, 0.99, 0.55])
        assert out[1] == "▁" and out[2] == "█"
        assert len(out) == 4
        assert set(out) <= set(TICKS)

    def test_descending_is_monotone(self):
        out = sparkline([1.0, 0.8, 0.6, 0.4, 0.2, 0.0])
        idx = [TICKS.index(c) for c in out]
        assert idx == sorted(idx, reverse=True)
        assert idx[0] == 7 and idx[-1] == 0

    @pytest.mark.parametrize("values", [[0.5, 0.5, 0.5], [3], [0, 0], [-2.0, -2.0]])
    def test_all_equal_values_do_not_divide_by_zero(self, values):
        assert sparkline(values) == "▁" * len(values)

    def test_none_gaps_become_spaces(self):
        out = sparkline([0.1, None, 0.5, None, None, 0.9])
        assert len(out) == 6
        assert out[1] == out[3] == out[4] == " "
        assert out[0] == "▁" and out[5] == "█"

    def test_gaps_do_not_affect_scaling(self):
        assert sparkline([None, 0, None, 7]) == " ▁ █"

    def test_negative_and_mixed_int_float(self):
        assert sparkline([-7, 0.0, -3.5]) == "▁█▄"
