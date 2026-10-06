"""Package A (fairvalue): race keys, pins, manual file, Polymarket/Kalshi parsing against tests/fixtures/external, combining, service. See docs/PAPER_TRADING.md §4.10."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx
import pytest

from supermarket_bot import fairvalue as fv
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.fairvalue import (
    FV_CAVEATS,
    FairValueService,
    KalshiProvider,
    ManualFairValues,
    MatchMap,
    MatchTarget,
    PolymarketProvider,
    ProviderResult,
    blend,
    build_targets,
    combine,
    import_history,
    kalshi_ticker_matches,
    known_unmatched_races,
    normalise_race,
    override_snippet,
    parse_probability,
    parse_race,
    party_of,
    pins_by_race,
    race_named_in,
    races_for,
    renormalise_combined,
    uncertainty_of,
    venue_value,
)
from supermarket_bot.models import ExchangeInfo, FairValue, FairValueQuote, FairValueRecord, FairValueRefresh, RaceRef
from supermarket_bot.readonly import ReadOnlyViolation

FIXTURES = Path(__file__).parent / "fixtures" / "external"
T0 = 1791144000.0  # 2026-10-04T20:00:00Z, the fixtures' snapshot time
GAMMA = "gamma-api.polymarket.com"
KALSHI_HOSTS = ("api.elections.kalshi.com", "external-api.kalshi.com")
LIST_PARAMS = ("tickers", "market_tickers")  # comma lists: compared as sets by the test transport


# --------------------------------------------------------------------------- test plumbing


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def _host(host: str) -> str:
    return "kalshi" if host in KALSHI_HOSTS else host


def _query_key(url: httpx.URL) -> Tuple[Tuple[str, str], ...]:
    """D57: the query as a multiset of (name, value) pairs, ignoring ``limit`` and the order."""
    items = []
    for name, value in url.params.multi_items():
        if name == "limit":
            continue
        if name in LIST_PARAMS:
            value = ",".join(sorted(value.split(",")))
        items.append((name, value))
    return tuple(sorted(items))


class FixtureTransport:
    """Answers from tests/fixtures/external with the D57 rule; also emulates Gamma ``/markets?id=`` and Kalshi
    ``/markets?tickers=`` (the real APIs filter by id) over every market object in the loaded fixtures. A request
    nothing answers gets a 404. Every request is recorded in ``calls``."""

    def __init__(self, *files: str, emulate: bool = True) -> None:
        self.calls: List[httpx.Request] = []
        self.exact: Dict[Tuple[str, str, Tuple[Tuple[str, str], ...]], Tuple[int, Any, Dict[str, str]]] = {}
        self.gamma_pool: Dict[str, Dict[str, Any]] = {}
        self.kalshi_pool: Dict[str, Dict[str, Any]] = {}
        self.emulate = emulate
        self.prefer_pool = False
        self.fail: Optional[Callable[[httpx.Request], Optional[Exception]]] = None
        self.before: Optional[Callable[[httpx.Request], None]] = None
        for name in files:
            self.add_file(name)

    def add_file(self, name: str) -> None:
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for r in data["responses"]:
            method, url = r["request"].split(" ", 1)
            assert method == "GET"
            u = httpx.URL(url)
            self.exact.setdefault((_host(u.host), u.path, _query_key(u)), (r["status"], r["body"], r.get("headers") or {}))
            if r["status"] == 200:
                self._pool(u, r["body"])

    def _pool(self, u: httpx.URL, body: Any) -> None:
        if u.host == GAMMA and u.path == "/markets" and isinstance(body, list):
            for m in body:
                self.gamma_pool.setdefault(str(m["id"]), m)
        if u.host == GAMMA and u.path == "/events" and isinstance(body, list):
            for ev in body:
                for m in ev.get("markets") or []:
                    m2 = dict(m)
                    m2.setdefault("events", [{"id": ev["id"], "title": ev["title"], "slug": ev["slug"], "endDate": ev.get("endDate")}])
                    self.gamma_pool.setdefault(str(m2["id"]), m2)
        if _host(u.host) == "kalshi" and isinstance(body, dict):
            for m in body.get("markets") or []:
                if isinstance(m, dict) and m.get("ticker"):
                    self.kalshi_pool.setdefault(m["ticker"], m)
            events = body.get("events") or ([body["event"]] if isinstance(body.get("event"), dict) else [])
            for ev in events:
                for m in ev.get("markets") or []:
                    m2 = dict(m)
                    m2.setdefault("event_ticker", ev.get("event_ticker"))
                    self.kalshi_pool.setdefault(m2["ticker"], m2)

    def patch_gamma(self, market_id: str, **fields: Any) -> None:
        self.gamma_pool[market_id] = dict(self.gamma_pool[market_id], **fields)
        self.prefer_pool = True

    def patch_kalshi(self, ticker: str, **fields: Any) -> None:
        self.kalshi_pool[ticker] = dict(self.kalshi_pool[ticker], **fields)
        self.prefer_pool = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.before is not None:
            self.before(request)
        if self.fail is not None:
            exc = self.fail(request)
            if exc is not None:
                raise exc
        u = request.url
        steady = (u.host == GAMMA and u.path == "/markets") or (_host(u.host) == "kalshi" and u.path.endswith("/markets"))
        key = (_host(u.host), u.path, _query_key(u))
        if key in self.exact and not (steady and self.prefer_pool and self.exact[key][0] == 200):
            status, body, headers = self.exact[key]
            return httpx.Response(status, json=body, headers=headers)
        if self.emulate and u.host == GAMMA and u.path == "/markets":
            ids = u.params.get_list("id")
            return httpx.Response(200, json=[self.gamma_pool[i] for i in ids if i in self.gamma_pool])
        if self.emulate and _host(u.host) == "kalshi" and u.path.endswith("/markets") and "tickers" in u.params:
            tickers = u.params["tickers"].split(",")
            return httpx.Response(200, json={"markets": [self.kalshi_pool[t] for t in tickers if t in self.kalshi_pool],
                                             "cursor": ""})
        return httpx.Response(404, json={"error": f"no fixture for {u}"})

    @property
    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> List[str]:
        return [r.url.path for r in self.calls]


def assert_get_only(transport: FixtureTransport) -> None:
    """§4.10.9: every request is an anonymous GET."""
    assert transport.calls, "expected at least one request"
    for request in transport.calls:
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


def _cup_rows() -> List[Dict[str, str]]:
    lines = [line for line in (FIXTURES / "supermarket_titles.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


CUP = _cup_rows()


def info(race_key: str, party: str) -> ExchangeInfo:
    row = next(r for r in CUP if r["race_key"] == race_key and r["party"] == party)
    return ExchangeInfo(exchange_id=row["sig_exchange_id"], market_id=row["sig_market_id"], option="YES",
                        market_title=row["title"])


def infos_for(*race_keys: str) -> List[ExchangeInfo]:
    return [info(r["race_key"], r["party"]) for r in CUP if r["race_key"] in race_keys]


def targets_for(*race_keys: str, overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[MatchTarget]:
    infos = infos_for(*race_keys)
    return build_targets(infos, races_for(infos, overrides), overrides)


def by_eid(targets: Sequence[MatchTarget]) -> Dict[str, MatchTarget]:
    return {t.exchange_id: t for t in targets}


def pm_provider(transport: FixtureTransport, clock: Optional[FakeClock] = None, **kw: Any) -> PolymarketProvider:
    clock = clock or FakeClock()
    return PolymarketProvider(transport=transport.mock, clock=clock, sleep=clock.advance, **kw)


def ks_provider(transport: FixtureTransport, clock: Optional[FakeClock] = None, **kw: Any) -> KalshiProvider:
    clock = clock or FakeClock()
    return KalshiProvider(transport=transport.mock, clock=clock, sleep=clock.advance, **kw)


def all_fixtures() -> FixtureTransport:
    return FixtureTransport("polymarket_markets.json", "polymarket_events.json", "kalshi_markets.json", "kalshi_events.json")


NH = "2026:SENATE:NH"
ME = "2026:SENATE:ME"
NH_D, NH_R, ME_D, ME_R = "1070", "1071", "959", "960"


def write_map(directory: Path, overrides: Mapping[str, Any], *, bump: float = 0.0) -> Path:
    path = directory / fv.MAP_FILE
    doc: Dict[str, Any] = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    doc["version"] = 1
    doc["overrides"] = dict(overrides)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    if bump:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(bump * 1e9)))
    return path


def q(venue: str, value: float, *, spread: float = 0.01, fetched_at: float = T0, kind: str = "EXACT",
      conf: float = 0.95, flags: Optional[List[str]] = None) -> FairValueQuote:
    return FairValueQuote(venue=venue, external_id=f"{venue}:x", bid=value - spread / 2, ask=value + spread / 2,
                          raw_value=value, value=value, spread=spread, fetched_at=fetched_at, match_kind=kind,
                          match_confidence=conf, flags=list(flags or []))


class ScriptedProvider:
    """A provider whose quotes the test sets (bid, ask) per exchange id; counts its calls."""

    def __init__(self, name: str = "polymarket", kind: str = "EXACT") -> None:
        self.name = name
        self.kind = kind
        self.quotes: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self.fetched_offset = 0.0
        self.calls = 0
        self.seen: List[List[str]] = []
        self.raise_error: Optional[Exception] = None
        self.closed = False

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        self.calls += 1
        self.seen.append([t.exchange_id for t in targets])
        if self.raise_error is not None:
            raise self.raise_error
        res = ProviderResult(venue=self.name, status="ok")
        for t in targets:
            if t.exchange_id in self.quotes:
                bid, ask = self.quotes[t.exchange_id]
                res.quotes[t.exchange_id] = FairValueQuote(
                    venue=self.name, external_id=f"{self.name}:{t.exchange_id}", bid=bid, ask=ask,
                    fetched_at=now - self.fetched_offset, match_kind=self.kind, match_confidence=1.0)
        return res

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- 1. race keys


def test_parse_race_all_237_cup_titles() -> None:
    assert len(CUP) == 237
    for row in CUP:
        ref = parse_race(row["title"])
        assert ref is not None, row["title"]
        assert (ref.race_key, ref.party) == (row["race_key"], row["party"]), row["title"]
        assert ref.race_key.startswith("2026:")
        assert ref.source == "title"


@pytest.mark.parametrize("title, option, key, office, state, district, party, source", [
    ("Will the Democratic Party win the New Hampshire Senate?", None, "2026:SENATE:NH", "SENATE", "NH", None, "D", "title"),
    ("Will the Republican Party win the Georgia Governor?", None, "2026:GOVERNOR:GA", "GOVERNOR", "GA", None, "R", "title"),
    ("Will the Democratic Party win the AZ-01 House race?", None, "2026:HOUSE:AZ-01", "HOUSE", "AZ", "AZ-01", "D", "title"),
    ("Will the Independent Party win the Nebraska Senate?", None, "2026:SENATE:NE", "SENATE", "NE", None, "I", "title"),
    ("Will the Democratic Party win the U.S. Senate?", None, "2026:SENATE_CONTROL:US", "SENATE_CONTROL", "US", None, "D", "title"),
    ("Will the Republican Party win the U.S. House?", None, "2026:HOUSE_CONTROL:US", "HOUSE_CONTROL", "US", None, "R", "title"),
    ("Will Democrats win the Michigan Senate race?", None, "2026:SENATE:MI", "SENATE", "MI", None, "D", "title"),
    ("Will Republicans win Ohio House District 9?", None, "2026:HOUSE:OH-09", "HOUSE", "OH", "OH-09", "R", "title"),
    ("Which party will control the Senate after the midterms?", "Republicans", "2026:SENATE_CONTROL:US", "SENATE_CONTROL", "US", None, "R", "option"),
    ("Which party will control the House after the midterms?", "Democrats", "2026:HOUSE_CONTROL:US", "HOUSE_CONTROL", "US", None, "D", "option"),
    ("Who will win the Arizona Governor race?", "Any other candidate", "2026:GOVERNOR:AZ", "GOVERNOR", "AZ", None, "O", "option"),
    ("Who will win the Arizona Governor race?", "Democratic nominee", "2026:GOVERNOR:AZ", "GOVERNOR", "AZ", None, "D", "option"),
])
def test_parse_race_spec_table(title: str, option: Optional[str], key: str, office: str, state: str,
                               district: Optional[str], party: str, source: str) -> None:
    ref = parse_race(title, option)
    assert ref is not None
    assert (ref.race_key, ref.office, ref.state, ref.district, ref.party, ref.source) == (key, office, state, district, party, source)


@pytest.mark.parametrize("title, option", [
    ("Will Republicans keep control of the Pennsylvania Senate?", None),
    ("How many House seats will Democrats win?", "218-229"),
    ("Will a Libertarian candidate win a U.S. House seat?", "YES"),
    ("Will the Georgia Senate candidates hold a televised debate?", "YES"),
    ("Will the Maine Senate candidates debate before October 7?", "YES"),
    ("Will the Democratic Party win the Atlantis Senate?", None),  # not a state
    ("Will the Democratic Party win the ZZ-01 House race?", None),  # not a state code
    ("Which party will control the Senate after the midterms?", "Katie Hobbs"),  # option carries no party
    ("", None),
])
def test_parse_race_none_cases(title: str, option: Optional[str]) -> None:
    assert parse_race(title, option) is None


def test_parse_race_whitespace_and_case() -> None:
    ref = parse_race("  Will the   Democratic Party win the  New Hampshire   Senate? ")
    assert ref is not None and ref.race_key == "2026:SENATE:NH"
    for option, party in [("  republicans ", "R"), ("DEMOCRATS", "D"), ("the Republican Party", "R"),
                          ("any OTHER   candidate", "O"), ("Katie Hobbs (D)", "D"), ("Osborn (I)", "I")]:
        ref = parse_race("Which party will control the House after the midterms?", option)
        assert ref is not None and ref.party == party, option
    ref = parse_race("Who will win the Arizona Governor race?", "Katie Hobbs (D)")
    assert ref is not None and ref.candidate == "Katie Hobbs"
    assert parse_race("Will the Democratic Party win the OH-9 House race?").district == "OH-09"  # type: ignore[union-attr]


def test_party_of() -> None:
    assert party_of("Chris Pappas (D)") == "D"
    assert party_of("John E. Sununu (R) ") == "R"
    assert party_of("Angie Craig (DFL)") == "D"
    assert party_of("Democrat") == "D"
    assert party_of("Republican Party") == "R"
    assert party_of("Independent") == "I"
    assert party_of("Someone else") == "O"
    assert party_of("Person A") is None
    assert party_of("") is None
    assert party_of(None) is None


def test_races_for_applies_overrides() -> None:
    infos = infos_for(NH) + [ExchangeInfo(exchange_id="9034", market_id="326", option="YES",
                                          market_title="Will the Maine Senate candidates debate before October 7?")]
    races = races_for(infos)
    assert races[NH_D].race_key == NH and "9034" not in races
    races = races_for(infos, {"9034": {"race_key": "2026:SENATE:ME", "party": "D"}, NH_R: {"party": "I"}})
    assert races["9034"] == RaceRef(race_key=ME, office="SENATE", state="ME", party="D", source="override")
    assert races[NH_R].party == "I" and races[NH_R].source == "override" and races[NH_R].race_key == NH


# --------------------------------------------------------------------------- 2. pins and targets


def test_pins_by_race_counts() -> None:
    pins = pins_by_race()
    assert len(pins) == 237
    assert sum(1 for v in pins.values() if v.get("polymarket")) == 231
    assert sum(1 for v in pins.values() if v.get("kalshi")) == 220
    assert {(r["race_key"], r["party"]) for r in CUP} == set(pins)
    near = {k for k, v in pins.items() if v["match_kind"] == "NEAR"}
    control = {k for k in pins if ":SENATE_CONTROL:" in k[0] or ":HOUSE_CONTROL:" in k[0]}
    independents = {k for k in pins if k[1] == "I"}
    assert near == control | independents
    assert len(control) == 4 and len(independents) == 3
    assert "kalshi" not in pins[("2026:SENATE:KY", "D")] and "kalshi" not in pins[("2026:SENATE:KY", "R")]  # blanked (LA tickers)
    pins[(NH, "D")]["polymarket"] = "mutated"
    assert pins_by_race()[(NH, "D")]["polymarket"] == "630844"  # a copy


def test_known_unmatched_races() -> None:
    assert known_unmatched_races() == frozenset({"2026:GOVERNOR:AK"})


def test_every_kalshi_pin_matches_its_own_race_and_not_another_state() -> None:
    checked = 0
    for (race_key, party), row in pins_by_race().items():
        ticker = row.get("kalshi")
        if not ticker:
            continue
        race = fv._race_from_key(race_key, party)
        assert race is not None
        assert kalshi_ticker_matches(ticker, race), (race_key, ticker)
        other_state = "ME" if race.state != "ME" else "NH"
        if race.office == "HOUSE":
            other = fv._race_from_key(f"2026:HOUSE:{other_state}-02", party)
        elif race.office in ("SENATE", "GOVERNOR"):
            other = fv._race_from_key(f"2026:{race.office}:{other_state}", party)
        else:  # chamber control: the other chamber
            other = fv._race_from_key("2026:HOUSE_CONTROL:US" if race.office == "SENATE_CONTROL" else "2026:SENATE_CONTROL:US", party)
        assert other is not None and not kalshi_ticker_matches(ticker, other), (race_key, ticker)
        checked += 1
    assert checked == 220
    la = fv._race_from_key("2026:SENATE:LA", "D")
    ky = fv._race_from_key("2026:SENATE:KY", "D")
    assert kalshi_ticker_matches("SENATELA-26-D", la) and not kalshi_ticker_matches("SENATELA-26-D", ky)  # type: ignore[arg-type]


def test_race_named_in() -> None:
    nh = fv._race_from_key(NH, "D")
    me02 = fv._race_from_key("2026:HOUSE:ME-02", "D")
    va = fv._race_from_key("2026:SENATE:VA", "D")
    wv = fv._race_from_key("2026:SENATE:WV", "D")
    ctl = fv._race_from_key("2026:SENATE_CONTROL:US", "D")
    ga_gov = fv._race_from_key("2026:GOVERNOR:GA", "R")
    assert race_named_in("New Hampshire Senate Election Winner", nh)  # type: ignore[arg-type]
    assert race_named_in("Will the Democrats win the new hampshire senate race in 2026?", nh)  # type: ignore[arg-type]
    assert not race_named_in("Maine Senate Election Winner", nh)  # type: ignore[arg-type]
    assert not race_named_in("New Hampshire Governor Election Winner", nh)  # type: ignore[arg-type]
    assert race_named_in("Will the Democratic Party win the ME-02 House seat?", me02)  # type: ignore[arg-type]
    assert race_named_in("ME-2 House winner", me02)  # type: ignore[arg-type]
    assert not race_named_in("ME-01 House Election Winner", me02)  # type: ignore[arg-type]
    assert race_named_in("West Virginia Senate Election Winner", wv)  # type: ignore[arg-type]
    assert not race_named_in("West Virginia Senate Election Winner", va)  # type: ignore[arg-type]
    assert race_named_in("Which party will win the Senate in 2026?", ctl)  # type: ignore[arg-type]
    assert not race_named_in("New Hampshire Senate Election Winner", ctl)  # type: ignore[arg-type]
    assert race_named_in("Will the Republicans win the Georgia governor race in 2026?", ga_gov)  # type: ignore[arg-type]
    assert not race_named_in(None, nh)  # type: ignore[arg-type]


def test_build_targets_pins_and_overrides() -> None:
    plain = by_eid(targets_for(NH, "2026:SENATE_CONTROL:US", "2026:SENATE:NE", "2026:GOVERNOR:AK"))
    assert plain[NH_D].pins == {"polymarket": "630844", "kalshi": "SENATENH-26-D"}
    assert plain[NH_D].pin_kind == "EXACT" and plain[NH_D].pin_sources == {"polymarket": "pin", "kalshi": "pin"}
    assert plain["842"].pin_kind == "NEAR" and plain["969"].pin_kind == "NEAR"
    assert plain["969"].pins == {"polymarket": "634893"}  # no Kalshi ticker for the Independent leg
    assert plain["1057"].pins == {}  # Alaska Governor
    overrides = {NH_D: {"polymarket": "999", "kalshi": None, "confirmed": True, "trade_near": True},
                 NH_R: {"disabled": True}}
    t = by_eid(targets_for(NH, overrides=overrides))
    assert t[NH_D].pins == {"polymarket": "999"} and t[NH_D].pin_sources == {"polymarket": "override", "kalshi": "override"}
    assert t[NH_D].confirmed and t[NH_D].trade_near and not t[NH_D].disabled
    assert t[NH_R].disabled and t[NH_R].pins == {"polymarket": "630845", "kalshi": "SENATENH-26-R"}
    nr = build_targets([ExchangeInfo("9034", "326", "YES", "Will the Maine Senate candidates debate before October 7?")], {})
    assert nr[0].race is None and nr[0].pins == {}


# --------------------------------------------------------------------------- 3. manual file


def test_parse_probability() -> None:
    assert parse_probability(0.55) == 0.55
    assert parse_probability("55%") == pytest.approx(0.55)
    assert parse_probability(" 58 % ") == pytest.approx(0.58)
    assert parse_probability(55) == pytest.approx(0.55)
    assert parse_probability("0.4") == pytest.approx(0.4)
    assert parse_probability(1) == 1.0 and parse_probability(0) == 0.0
    assert parse_probability(100) == 1.0
    assert parse_probability(None) is None and parse_probability("") is None and parse_probability("  ") is None
    for bad in ("abc", -0.1, 101, "120%", True, [0.5], float("nan")):
        with pytest.raises(ValueError):
            parse_probability(bad)


def _manual(tmp_path: Path, doc: Any, *, name: str = fv.MANUAL_JSON, clock: Optional[FakeClock] = None) -> ManualFairValues:
    path = tmp_path / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return ManualFairValues(tmp_path, clock=clock or FakeClock())


def test_manual_json_precedence_and_fields(tmp_path: Path) -> None:
    fresh = "2026-10-04T12:00:00Z"
    doc = {"version": 1, "values": [
        {"match": "New Hampshire Senate", "party": "D", "probability": 0.40, "updated_at": fresh},
        {"race_key": "2026:senate:nh", "party": "D", "probability": "58%", "source": "my model", "updated_at": fresh},
        {"exchange_id": NH_D, "probability": 0.55, "uncertainty": 0.03, "source": "Silver Bulletin",
         "note": "model 57%, shaded", "updated_at": fresh},
        {"race_key": NH, "party": "R", "probability": 45, "updated_at": fresh},
        {"match": "re:^Will the Republican Party win the Maine Senate\\?$", "probability": 0.6, "updated_at": fresh},
        {"race_key": NH, "party": "R", "probability": 0.47, "updated_at": fresh},  # same precedence, later wins
    ]}
    manual = _manual(tmp_path, doc)
    targets = targets_for(NH, ME)
    values = manual.resolve(targets, T0)
    assert values[NH_D].value == 0.55 and values[NH_D].uncertainty == 0.03
    assert values[NH_D].source == "manual" and values[NH_D].match_kind == "MANUAL" and values[NH_D].confidence == "medium"
    assert values[NH_D].match_confidence == 1.0 and values[NH_D].usable and not values[NH_D].suspect
    assert values[NH_D].note == "model 57%, shaded"
    assert values[NH_D].manual_updated_at == values[NH_D].as_of == fv._parse_time(fresh)
    assert values[NH_R].value == pytest.approx(0.47) and values[NH_R].uncertainty == fv.MANUAL_UNCERTAINTY
    assert values[ME_R].value == 0.6 and ME_D not in values
    status = manual.status()
    assert status["exists"] and status["entries"] == 6
    assert any("overrides" in e for e in status["errors"])  # the later NH R entry replaced the earlier one


def test_manual_csv_comments_percent_and_uncertainty(tmp_path: Path) -> None:
    text = ("# my numbers\n"
            "exchange_id,race_key,party,match,probability,uncertainty,source,note,updated_at\n"
            "\n"
            "1070,,,,57%,3%,model,\"shaded, a bit\",2026-10-04T12:00:00Z\n"
            "# a comment line\n"
            ",2026:SENATE:ME,R,,0.41,,,,1791100000\n"
            "1071,,,,,,,,\n")  # a template row
    manual = _manual(tmp_path, text, name=fv.MANUAL_CSV)
    assert manual.path.name == fv.MANUAL_CSV
    values = manual.resolve(targets_for(NH, ME), T0)
    assert values[NH_D].value == pytest.approx(0.57) and values[NH_D].uncertainty == pytest.approx(0.03)
    assert values[NH_D].note == "shaded, a bit"
    assert values[ME_R].value == pytest.approx(0.41) and values[ME_R].as_of == 1791100000.0
    assert NH_R not in values
    assert manual.status()["errors"] == []


def test_manual_json_wins_over_csv(tmp_path: Path) -> None:
    (tmp_path / fv.MANUAL_CSV).write_text("exchange_id,probability\n1070,0.2\n", encoding="utf-8")
    manual = ManualFairValues(tmp_path, clock=FakeClock())
    assert manual.path.name == fv.MANUAL_CSV
    (tmp_path / fv.MANUAL_JSON).write_text(json.dumps({"values": [{"exchange_id": NH_D, "probability": 0.6}]}), encoding="utf-8")
    assert manual.path.name == fv.MANUAL_JSON
    assert manual.resolve(targets_for(NH), T0)[NH_D].value == 0.6


def test_manual_ambiguous_match_is_an_error(tmp_path: Path) -> None:
    manual = _manual(tmp_path, {"values": [{"match": "Senate", "probability": 0.5}, {"match": "Maine Senate", "probability": 0.5}]})
    values = manual.resolve(targets_for(NH, ME), T0)
    assert values == {}
    errors = manual.status()["errors"]
    assert any("ambiguous: matches 4 outcomes" in e for e in errors)
    assert any("ambiguous: matches 2 outcomes" in e for e in errors)


def test_manual_stale_entry_unusable_and_mtime_default(tmp_path: Path) -> None:
    old = T0 - fv.MANUAL_MAX_AGE_S - 60
    manual = _manual(tmp_path, {"values": [{"exchange_id": NH_D, "probability": 0.5, "updated_at": old},
                                           {"exchange_id": NH_R, "probability": 0.5}]})
    path = tmp_path / fv.MANUAL_JSON
    os.utime(path, (T0 - 3600, T0 - 3600))
    values = manual.resolve(targets_for(NH), T0)
    assert values[NH_D].usable is False and values[NH_D].reason == "manual value is older than 72 h; update updated_at"
    assert values[NH_R].usable is True and values[NH_R].as_of == pytest.approx(T0 - 3600)  # the file's mtime


def test_manual_template_rows_ignored_and_template_shape(tmp_path: Path) -> None:
    targets = targets_for(NH)
    manual = ManualFairValues(tmp_path, clock=FakeClock())
    doc = manual.template(targets)
    assert doc["version"] == 1 and len(doc["values"]) == 2
    assert doc["values"][0] == {"exchange_id": NH_D, "title": "Will the Democratic Party win the New Hampshire Senate?",
                                "race_key": NH, "party": "D", "probability": None, "source": "", "note": "", "updated_at": None}
    (tmp_path / fv.MANUAL_JSON).write_text(json.dumps(doc), encoding="utf-8")
    assert manual.resolve(targets, T0) == {}
    assert manual.status()["errors"] == [] and manual.status()["entries"] == 0


def test_manual_reloads_on_mtime_change(tmp_path: Path) -> None:
    manual = _manual(tmp_path, {"values": [{"exchange_id": NH_D, "probability": 0.5}]})
    first = manual.load()
    assert manual.load() is first  # unchanged file: no re-read
    path = tmp_path / fv.MANUAL_JSON
    path.write_text(json.dumps({"values": [{"exchange_id": NH_D, "probability": 0.7}]}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert manual.load() is not first
    assert manual.resolve(targets_for(NH), T0)[NH_D].value == 0.7


def test_manual_problems_are_reported_never_raised(tmp_path: Path) -> None:
    manual = _manual(tmp_path, "{not json")
    assert manual.resolve(targets_for(NH), T0) == {}
    assert any("not valid JSON" in e for e in manual.status()["errors"])
    manual = _manual(tmp_path, {"values": [
        {"exchange_id": NH_D, "probability": "lots"},
        {"exchange_id": "424242", "probability": 0.5},
        {"exchange_id": NH_R, "probabilty": 0.5},
        {"probability": 0.5},
        {"race_key": NH, "probability": 0.5},
        {"race_key": NH, "party": "X", "probability": 0.5},
        {"exchange_id": NH_R, "probability": 0.5, "updated_at": "yesterday"},
        "not an object",
    ], "extra": 1})
    assert manual.resolve(targets_for(NH), T0) == {}
    errors = " | ".join(manual.status()["errors"])
    for needle in ("'lots' is not a number", "424242 is not an open Cup outcome", "unknown key(s) 'probabilty'",
                   "needs exchange_id, race_key + party, or match", "race_key needs a party", "party 'X'",
                   "updated_at 'yesterday'", "not an object", "unknown top-level key(s) 'extra'"):
        assert needle in errors, needle
    missing = ManualFairValues(tmp_path / "nowhere", clock=FakeClock())
    assert missing.load().exists is False and missing.resolve(targets_for(NH), T0) == {}


# --------------------------------------------------------------------------- 4. Polymarket


def test_polymarket_nh_real_capture() -> None:
    transport = all_fixtures()
    clock = FakeClock()
    provider = pm_provider(transport, clock)
    res = provider.refresh(targets_for(NH), T0, deadline=T0 + 30)
    assert res.status == "ok" and res.requests == 1 and res.errors == []
    d, r = res.quotes[NH_D], res.quotes[NH_R]
    assert (d.bid, d.ask, d.last, d.spread) == (0.85, 0.86, 0.85, 0.01)
    assert d.raw_value == pytest.approx(0.855) and r.raw_value == pytest.approx(0.135)
    assert d.fetched_at == T0 and d.venue_updated_at == pytest.approx(fv._parse_time("2026-10-01T00:19:33.396847Z"))
    match = res.matches[NH_D]
    assert match.external_id == "630844" and match.kind == "EXACT" and match.confidence == fv.CONF_PIN_EXACT
    assert match.label == "Chris Pappas (D) - New Hampshire Senate Election Winner" and match.source == "pin"
    assert match.reason == ("Pinned Polymarket market 630844 (participant map), validated live: active, ends 2026-11-04, "
                            "names the New Hampshire Senate, 'Chris Pappas (D)' is the Democratic leg.")
    assert match.token_id and match.token_id.startswith("824100586911") and match.validated_at == T0
    req = transport.calls[0]
    assert req.url.path == "/markets" and sorted(req.url.params.get_list("id")) == ["630844", "630845"]
    assert req.url.params["limit"] == "2"
    assert_get_only(transport)


def test_polymarket_exact_fixture_request_and_every_check() -> None:
    """The fixture's 10-id /markets request (D57 exact match): NH live legs, a placeholder, Maine, chamber control
    (NEAR), ME-02 (slug says me-01; wide spread -> last trade), the NE Independent (NEAR) and a closed primary."""
    transport = FixtureTransport("polymarket_markets.json", emulate=False)
    overrides = {"983": {"polymarket": "601102"},  # TX Senate R pinned to a closed primary market
                 "982": {"polymarket": "630846"}}  # TX Senate D pinned to the inactive "Person A" placeholder
    targets = targets_for(NH, ME, "2026:SENATE_CONTROL:US", "2026:HOUSE:ME-02", "2026:SENATE:NE", "2026:SENATE:TX",
                          overrides=overrides)
    wanted = {t.pins["polymarket"] for t in targets if "polymarket" in t.pins}
    targets = [t for t in targets if t.pins.get("polymarket") in
               {"630844", "630845", "630846", "630772", "630773", "562793", "562794", "3006483", "634893", "601102"}]
    assert len(targets) == 10 and len(wanted) >= 10
    res = pm_provider(transport).refresh(targets, T0)
    assert transport.calls[0].url.params["limit"] == "10" and res.status == "ok"
    assert res.quotes["842"].match_kind == "NEAR" and res.matches["842"].confidence == fv.CONF_PIN_NEAR
    me02 = res.quotes["910"]
    assert me02.raw_value == pytest.approx(0.44) and "last-trade-only" in me02.flags and "wide" in me02.flags
    assert res.matches["910"].label == "Matthew Dunlap (D) - ME-02 House Election Winner"
    assert res.quotes["969"].match_kind == "NEAR"
    assert "is not active" in res.rejected["982"] and "closed" in res.rejected["983"]
    assert "982" not in res.quotes and "983" not in res.quotes
    assert res.rejected["982"].startswith("Your pinned Polymarket market 630846")
    assert_get_only(transport)


def test_polymarket_string_encoded_and_already_decoded_fields() -> None:
    transport = all_fixtures()
    transport.patch_gamma("630844", outcomes=["Yes", "No"], bestBid="0.84", bestAsk="0.86", lastTradePrice="0.85",
                          spread="0.02", clobTokenIds=["111", "222"])
    transport.patch_gamma("630845", bestBid=None, bestAsk=None, outcomePrices="[\"0.155\", \"0.845\"]")
    res = pm_provider(transport).refresh(targets_for(NH), T0)
    d, r = res.quotes[NH_D], res.quotes[NH_R]
    assert (d.bid, d.ask, d.spread, d.raw_value) == (0.84, 0.86, 0.02, pytest.approx(0.85))
    assert res.matches[NH_D].token_id == "111"
    assert r.raw_value == pytest.approx(0.155) and r.flags == ["last-trade-only"]


def test_polymarket_active_placeholder_quote_has_no_value() -> None:
    transport = all_fixtures()
    transport.patch_gamma("630844", bestBid=0, bestAsk=1, lastTradePrice=0, spread=1)
    res = pm_provider(transport).refresh(targets_for(NH), T0)
    assert res.quotes[NH_D].raw_value is None and res.quotes[NH_D].flags == ["placeholder"]
    out = normalise_race({k: v for k, v in res.quotes.items()}, races_for(infos_for(NH)))
    assert out[NH_R].value == pytest.approx(0.135)  # the placeholder leg is not quoted and does not poison the sum
    fair = blend(NH_D, [out[NH_D]], T0)
    assert fair.value is None and not fair.usable and "placeholder" in fair.reason


def test_polymarket_outcomes_not_yes_no_rejected() -> None:
    transport = all_fixtures()
    transport.patch_gamma("630844", outcomes="[\"No\", \"Yes\"]")
    res = pm_provider(transport).refresh(targets_for(NH), T0)
    assert NH_D not in res.quotes and "not [\"Yes\", \"No\"]" in res.rejected[NH_D]


def test_polymarket_margin_and_2028_markets_rejected() -> None:
    transport = all_fixtures()
    overrides = {NH_D: {"polymarket": "3343938"}, NH_R: {"polymarket": "561229"}}
    res = pm_provider(transport).refresh(targets_for(NH, overrides=overrides), T0)
    assert "belongs to 'New Hampshire Senate Election: Margin of Victory', not a winner market" in res.rejected[NH_D]
    assert "ends in 2028, not 2026" in res.rejected[NH_R]
    assert res.quotes == {}
    provider = pm_provider(transport)
    ctl = fv._race_from_key("2026:SENATE_CONTROL:US", "D")
    events = json.loads((FIXTURES / "polymarket_events.json").read_text())["responses"][0]["body"]
    by_title = {e["title"]: e for e in events}
    assert provider._event_is_race(by_title["Which party will win the Senate in 2026?"], ctl)
    assert not provider._event_is_race(by_title["New Hampshire Senate Election: Margin of Victory"], fv._race_from_key(NH, "D"))
    assert not provider._event_is_race(by_title["Presidential Election Winner 2028"], ctl)


def test_polymarket_discovery_matches_and_fails_closed() -> None:
    """Races without a valid pin are searched in /events (bare array): NE Senate D via its party label, the GA Governor;
    Alaska Senate is candidate-only on Polymarket (fails closed); the ME-02 slug mislabel does not matter."""
    transport = all_fixtures()
    overrides = {"855": {"polymarket": None}}  # GA Governor D: the user removed its pin -> never discovered
    targets = targets_for("2026:SENATE:AK", "2026:GOVERNOR:GA", "2026:HOUSE:ME-02", overrides=overrides)
    # drop the fv_pins ids of GA R and ME-02 so that discovery has to find them
    for t in targets:
        if t.exchange_id in ("856", "910", "911"):
            t.pins.pop("polymarket", None)
    clock = FakeClock()
    provider = pm_provider(transport, clock)
    res = provider.refresh(targets, T0)
    assert res.status == "ok"
    assert "/events" in transport.paths()
    events_req = next(r for r in transport.calls if r.url.path == "/events")
    assert fv._norm_ws(str(events_req.url.query)) and events_req.url.params["tag_slug"] == "midterms"
    assert events_req.url.params["offset"] == "0" and events_req.url.params["closed"] == "false"
    assert res.matches["856"].source == "discovery" and res.matches["856"].confidence == fv.CONF_DISCOVERY
    assert res.matches["856"].external_id == "629338"
    assert res.matches["910"].external_id == "3006483" and res.matches["911"].external_id == "3006482"
    assert "855" not in res.matches and "855" not in res.rejected
    assert "1066" not in res.quotes and "candidate names only" in res.rejected["1066"]
    # discovery is not repeated within DISCOVERY_EVERY_S; discovered ids are read in the steady state
    n_events = transport.paths().count("/events")
    res2 = provider.refresh(targets, T0 + 60)
    assert transport.paths().count("/events") == n_events
    assert res2.quotes["856"].raw_value == pytest.approx(0.435)
    assert "629338" in transport.calls[-1].url.params.get_list("id")
    assert_get_only(transport)


def test_polymarket_discovery_pages_until_a_short_page() -> None:
    events = json.loads((FIXTURES / "polymarket_events.json").read_text())["responses"][0]["body"]
    filler = [{"id": str(900000 + i), "title": f"Filler event {i}", "slug": f"filler-{i}", "endDate": "2026-11-04T00:00:00Z",
               "markets": []} for i in range(fv.DISCOVERY_PAGE_LIMIT)]
    offsets: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            offsets.append(request.url.params["offset"])
            page = filler if request.url.params["offset"] == "0" else events
            return httpx.Response(200, json=page)
        return httpx.Response(200, json=[])

    provider = PolymarketProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    targets = targets_for("2026:GOVERNOR:GA")
    for t in targets:
        t.pins.clear()
    res = provider.refresh(targets, T0)
    assert offsets == ["0", "100"]
    assert res.matches["855"].external_id == "629337" and res.matches["856"].external_id == "629338"


def test_polymarket_ids_are_chunked_by_50() -> None:
    transport = all_fixtures()
    race = fv._race_from_key(NH, "D")
    targets = [MatchTarget(exchange_id=str(i), market_id=str(i), title="t", race=race, pins={"polymarket": str(100000 + i)},
                           pin_sources={"polymarket": "override"}) for i in range(120)]
    res = pm_provider(transport).refresh(targets, T0)
    limits = [r.url.params["limit"] for r in transport.calls]
    assert limits == ["50", "50", "20"] and res.requests == 3
    assert all("not returned by Polymarket" in why for why in res.rejected.values())


# --------------------------------------------------------------------------- swapped pins (D50)


def test_swapped_pins_fail_closed(tmp_path: Path) -> None:
    overrides = {
        NH_D: {"polymarket": "630772", "kalshi": "SENATEME-26-D"},  # New Hampshire pinned to Maine's ids
        ME_D: {"polymarket": "630844", "kalshi": "SENATENH-26-D"},  # Maine pinned to New Hampshire's ids
        NH_R: {"polymarket": "630844", "kalshi": "SENATENH-26-D"},  # D <-> R swapped
        ME_R: {"polymarket": "630772", "kalshi": "SENATEME-26-D"},
    }
    write_map(tmp_path, overrides)
    transport = all_fixtures()
    clock = FakeClock()
    svc = FairValueService(tmp_path, providers=[pm_provider(transport, clock), ks_provider(transport, clock)], clock=clock)
    svc.set_targets(infos_for(NH, ME))
    snap = svc.refresh(T0)
    for eid in (NH_D, ME_D, NH_R, ME_R):
        value = snap.values[eid]
        assert value.value is None and value.usable is False, eid
    assert "Your pinned Polymarket market 630772 names Maine, not the New Hampshire Senate: dropped." in snap.values[NH_D].reason
    assert "Your pinned Kalshi ticker SENATEME-26-D is not the New Hampshire Senate" in snap.values[NH_D].reason
    assert "names New Hampshire, not the Maine Senate" in snap.values[ME_D].reason
    assert "is the Democratic leg ('Chris Pappas (D)'), not the Republican one" in snap.values[NH_R].reason
    assert "has rules that name the Democratic party, not the Republican party" in snap.values[NH_R].reason
    rows = {r.exchange_id: r for r in svc.matches()}
    assert rows[NH_R].matches == [] and "Republican" in (rows[NH_R].unmatched_reason or "")
    assert not any(r.url.path == "/events" for r in transport.calls)  # an override is never replaced by discovery
    assert_get_only(transport)


# --------------------------------------------------------------------------- 5. Kalshi


def test_kalshi_dollar_strings_real_nh_capture() -> None:
    transport = all_fixtures()
    res = ks_provider(transport).refresh(targets_for(NH), T0)
    assert res.status == "ok" and res.requests == 1
    d = res.quotes[NH_D]
    assert (d.bid, d.ask, d.last) == (0.86, 0.87, 0.86)
    assert d.raw_value == pytest.approx(0.865) and d.spread == pytest.approx(0.01)
    assert (d.bid_size, d.ask_size) == (2524.15, 27059.72)
    assert d.venue_updated_at == pytest.approx(fv._parse_time("2026-09-15T21:46:54.814624Z"))
    assert res.quotes[NH_R].venue_updated_at == pytest.approx(fv._parse_time("2026-09-15T21:46:55.77266Z"))
    m = res.matches[NH_D]
    assert m.external_id == "SENATENH-26-D" and m.label == "SENATENH-26-D - Chris Pappas" and m.source == "pin"
    assert "validated live: active, event SENATENH-26" in m.reason
    req = transport.calls[0]
    assert req.url.path == "/trade-api/v2/markets" and req.url.params["limit"] == "100"
    assert sorted(req.url.params["tickers"].split(",")) == ["SENATENH-26-D", "SENATENH-26-R"]
    assert_get_only(transport)


def test_kalshi_exact_fixture_request() -> None:
    transport = FixtureTransport("kalshi_markets.json", emulate=False)
    targets = targets_for(NH, ME, "2026:SENATE_CONTROL:US", "2026:HOUSE:AZ-01")
    res = ks_provider(transport).refresh(targets, T0)
    assert res.status == "ok" and len(res.quotes) == 8
    assert res.quotes["842"].match_kind == "NEAR"
    assert res.quotes["886"].raw_value == pytest.approx(0.615)


def test_kalshi_cents_are_never_read_as_dollars() -> None:
    dollars = KalshiProvider._dollars
    assert dollars({"yes_bid_dollars": "0.8600", "yes_bid": 86}, "yes_bid") == 0.86
    assert dollars({"yes_bid": 86}, "yes_bid") is None  # integer cents
    assert dollars({"yes_bid": "86"}, "yes_bid") is None
    assert dollars({"yes_bid": 0.86}, "yes_bid") == 0.86  # looks like dollars
    assert dollars({"yes_bid": "0.8600"}, "yes_bid") == 0.86
    assert dollars({"yes_bid": 1}, "yes_bid") is None  # ambiguous: one cent or one dollar
    assert dollars({}, "yes_bid") is None


def test_kalshi_discovery_nested_markets_and_cursor_paging() -> None:
    transport = all_fixtures()
    targets = targets_for("2026:SENATE:NE", "2026:GOVERNOR:AK")  # NE Senate has no Kalshi pin
    res = ks_provider(transport).refresh(targets, T0)
    pages = [r for r in transport.calls if r.url.path.endswith("/events")]
    assert len(pages) == 2 and "cursor" not in pages[0].url.params
    assert pages[1].url.params["cursor"] == "CgwI8LnUxwYQwP7NsQISC1NFTkFURU1FLTI2"
    assert pages[0].url.params["with_nested_markets"] == "true" and pages[0].url.params["status"] == "open"
    assert res.matches["970"].external_id == "SENATENE-26-R" and res.matches["970"].source == "discovery"
    assert res.quotes["970"].raw_value == pytest.approx(0.675)
    assert "969" not in res.matches  # an Independent leg is never discovered (it names a candidate, not a party)
    assert "No open Kalshi market for the Nebraska Senate" in res.rejected["968"]
    assert "1057" not in res.rejected  # the Alaska Governor is never searched for
    assert_get_only(transport)


def test_kalshi_discovery_top_level_markets() -> None:
    events = json.loads((FIXTURES / "kalshi_events.json").read_text())["responses"]
    nested = next(r["body"] for r in events if "cursor=" not in r["request"] and r["request"].endswith("with_nested_markets=true&limit=200")
                  and "series_ticker" not in r["request"])
    flat_events = [dict(e, markets=[]) for e in nested["events"]]
    flat_markets = [dict(m, event_ticker=e["event_ticker"]) for e in nested["events"] for m in e["markets"]]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": flat_events, "markets": flat_markets, "cursor": ""})
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    provider = KalshiProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    res = provider.refresh(targets_for("2026:SENATE:NE"), T0)
    assert res.matches["970"].external_id == "SENATENE-26-R"


def test_kalshi_rules_state_party_and_ticker_checks() -> None:
    transport = all_fixtures()
    overrides = {NH_D: {"kalshi": "SENATENH-26-R"}, NH_R: {"kalshi": "SENATEME-26-R"}}
    res = ks_provider(transport).refresh(targets_for(NH, overrides=overrides), T0)
    assert "has rules that name the Republican party, not the Democratic party" in res.rejected[NH_D]
    assert "Your pinned Kalshi ticker SENATEME-26-R is not the New Hampshire Senate (its event is SENATEME-26)" in res.rejected[NH_R]
    transport = all_fixtures()
    transport.patch_kalshi("SENATENH-26-D", rules_primary="If a representative of the Democratic party is sworn in as a "
                                                           "Senator of Maine for the term beginning in 2027, then Yes.")
    transport.patch_kalshi("SENATENH-26-R", status="finalized")
    res = ks_provider(transport).refresh(targets_for(NH), T0)
    assert "has rules that name Maine, not New Hampshire" in res.rejected[NH_D]
    assert "is finalized, not open" in res.rejected[NH_R]
    transport.patch_kalshi("SENATENH-26-R", status="active", event_ticker="SENATENH-24")
    res = ks_provider(transport).refresh(targets_for(NH), T0)
    assert "is not a 2026 event (SENATENH-24)" in res.rejected[NH_R]


def test_kalshi_404_is_an_error_sentence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})

    provider = KalshiProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    res = provider.refresh(targets_for(NH), T0)
    assert res.status == "error" and res.quotes == {}
    assert res.errors[0] == "Kalshi answered HTTP 404 for /trade-api/v2/markets: no outside fair value from Kalshi this time."
    assert res.next_try_at == T0 + fv.BACKOFF_S[0]


def test_429_retry_after_is_honoured_and_capped() -> None:
    transport = FixtureTransport("rate_limits.json", "kalshi_markets.json", "polymarket_markets.json")
    kalshi = ks_provider(transport)
    res = kalshi.refresh(targets_for(NH), T0)
    assert res.status == "backoff" and res.next_try_at == T0 + fv.MAX_RETRY_AFTER_S  # 900 s asked, capped at 600
    assert "asked us to slow down (HTTP 429): next try in 600 s" in res.errors[0]
    n = len(transport.calls)
    again = kalshi.refresh(targets_for(NH), T0 + 599)
    assert again.status == "backoff" and again.requests == 0 and len(transport.calls) == n
    poly = pm_provider(transport)
    res = poly.refresh(targets_for(NH), T0)
    assert res.status == "backoff" and res.next_try_at == T0 + 120


def test_kalshi_base_url_fallback_on_connection_error() -> None:
    transport = all_fixtures()
    transport.fail = lambda r: httpx.ConnectError("Name or service not known") if r.url.host == "api.elections.kalshi.com" else None
    provider = ks_provider(transport)
    res = provider.refresh(targets_for(NH), T0)
    assert res.status == "ok" and res.requests == 2 and len(res.quotes) == 2
    assert [r.url.host for r in transport.calls] == ["api.elections.kalshi.com", "external-api.kalshi.com"]
    assert provider.base_url == fv.KALSHI_BASE_URLS[1]
    res = provider.refresh(targets_for(NH), T0 + 60)  # the working host is kept
    assert res.status == "ok" and transport.calls[-1].url.host == "external-api.kalshi.com"


# --------------------------------------------------------------------------- 6. offline, deadline, backoff


@pytest.mark.parametrize("exc, text", [
    (httpx.ProxyError("403 Forbidden at CONNECT"), "is unreachable from this machine (connection refused by the network proxy)"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "is unreachable from this machine (could not connect)"),
    (httpx.ConnectTimeout("timed out"), "did not answer within 8 s"),
    (httpx.ReadTimeout("timed out"), "did not answer within 8 s"),
])
def test_offline_after_the_first_failure(exc: Exception, text: str) -> None:
    for make, name, label in ((pm_provider, "polymarket", "Polymarket"), (ks_provider, "kalshi", "Kalshi")):
        transport = all_fixtures()
        transport.fail = lambda r, exc=exc: exc
        race = fv._race_from_key(NH, "D")
        targets = [MatchTarget(exchange_id=str(i), market_id=str(i), title="t", race=race,
                               pins={"polymarket": str(i), "kalshi": f"SENATENH-26-{i}"}) for i in range(250)]
        provider = make(transport)
        res = provider.refresh(targets, T0, deadline=T0 + 30)
        assert res.status == "offline" and res.quotes == {} and res.matches == {}
        # one request per host: Kalshi tries its second base URL once on a plain connection error
        hosts = 2 if (name == "kalshi" and isinstance(exc, httpx.ConnectError)) else 1
        assert res.requests == hosts == len(transport.calls)
        assert res.errors[0].startswith(f"{label} ") and text in res.errors[0]
        assert res.errors[0].endswith(f"no outside fair value from {label}.")
        assert res.next_try_at == T0 + 30


def test_refresh_deadline_stops_new_requests() -> None:
    clock = FakeClock()
    transport = all_fixtures()
    transport.before = lambda r: clock.advance(20)  # every request takes 20 s
    race = fv._race_from_key(NH, "D")
    targets = [MatchTarget(exchange_id=str(i), market_id=str(i), title="t", race=race, pins={"polymarket": str(i)},
                           pin_sources={"polymarket": "override"}) for i in range(150)]
    res = pm_provider(transport, clock).refresh(targets, T0, deadline=T0 + fv.REFRESH_DEADLINE_S)
    assert res.requests == 2 and len(transport.calls) == 2  # at t=0 and t=20; none at t=40
    assert res.status == "partial" and "deadline" in res.errors[0]
    assert res.next_try_at is None


def test_a_full_limiter_never_waits_past_the_deadline() -> None:
    def no_sleep(seconds: float) -> None:
        raise AssertionError(f"the refresh waited {seconds} s for its limiter")

    transport = all_fixtures()
    race = fv._race_from_key(NH, "D")
    targets = [MatchTarget(exchange_id=str(i), market_id=str(i), title="t", race=race, pins={"polymarket": str(i)},
                           pin_sources={"polymarket": "override"}) for i in range(150)]
    provider = PolymarketProvider(transport=transport.mock, clock=FakeClock(), sleep=no_sleep, reads_per_min=2)
    res = provider.refresh(targets, T0, deadline=T0 + 30)
    assert res.requests == 2 and res.status == "partial"


def test_a_valid_user_override_pin_has_full_confidence() -> None:
    transport = all_fixtures()
    res = pm_provider(transport).refresh(targets_for(NH, overrides={NH_D: {"polymarket": "630844", "confirmed": True}}), T0)
    match = res.matches[NH_D]
    assert match.source == "override" and match.confidence == fv.CONF_OVERRIDE == 1.0
    assert match.reason.startswith("Your pinned Polymarket market 630844 (your override in fair_value_map.json), validated live")
    assert res.matches[NH_R].source == "pin"


def test_backoff_sequence_and_next_try_at() -> None:
    transport = all_fixtures()
    transport.fail = lambda r: httpx.ConnectError("refused")
    clock = FakeClock()
    provider = pm_provider(transport, clock)
    targets = targets_for(NH)
    now = T0
    waits = []
    for _ in range(6):
        res = provider.refresh(targets, now)
        assert res.status == "offline"
        waits.append(res.next_try_at - now)  # type: ignore[operator]
        during = provider.refresh(targets, now + 1)
        # live-4: while waiting after an offline failure the venue is still reported offline, cause first
        assert during.status == "offline" and during.requests == 0 and during.next_try_at == res.next_try_at
        assert during.errors[0] == res.errors[0] and "No request to Polymarket until" in during.errors[1]
        now = res.next_try_at  # type: ignore[assignment]
    assert waits == [30.0, 60.0, 120.0, 300.0, 600.0, 600.0]
    transport.fail = None
    res = provider.refresh(targets, now)
    assert res.status == "ok" and res.next_try_at is None and len(res.quotes) == 2
    transport.fail = lambda r: httpx.ConnectError("refused")
    assert provider.refresh(targets, now + 60).next_try_at == now + 60 + 30.0  # the sequence restarts


def test_server_error_and_bad_json_are_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "markets" in request.url.path and request.url.host == GAMMA:
            return httpx.Response(503, text="down")
        return httpx.Response(200, text="<html>not json</html>")

    poly = PolymarketProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    res = poly.refresh(targets_for(NH), T0)
    assert res.status == "error" and "server error (HTTP 503)" in res.errors[0]
    kalshi = KalshiProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    res = kalshi.refresh(targets_for(NH), T0)
    assert res.status == "error" and "not JSON" in res.errors[0]


def test_unexpected_transport_exception_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    poly = PolymarketProvider(transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=lambda s: None)
    res = poly.refresh(targets_for(NH), T0)
    assert res.status == "error" and "RuntimeError" in res.errors[0]


# --------------------------------------------------------------------------- 7. combining


def test_venue_value_rules() -> None:
    assert venue_value(0.85, 0.86, 0.85) == (pytest.approx(0.855), [])
    assert venue_value(0.40, 0.55, 0.44) == (0.44, ["wide", "last-trade-only"])
    assert venue_value(0.45, 0.55, None) == (pytest.approx(0.5), [])  # exactly 0.10 wide is still a mid
    assert venue_value(0.0, 1.0, 0.3) == (None, ["placeholder"])
    assert venue_value(None, None, 0.7) == (0.7, ["last-trade-only"])
    assert venue_value(None, None, None) == (None, ["placeholder"])
    assert venue_value(0.0, 0.2, None) == (None, ["wide"])
    assert venue_value(0.0, 0.05, None) == (None, [])
    assert venue_value(0.30, 0.50, 1.0) == (None, ["wide"])


def test_normalise_race_sum_band_and_placeholder_leg() -> None:
    races = races_for(infos_for("2026:SENATE:NE", NH))
    # NE: the "Democrat" leg is a placeholder (not quoted): R + I alone are normalised
    quotes = {"968": FairValueQuote(venue="polymarket", external_id="634891", flags=["placeholder"]),
              "970": q("polymarket", 0.705), "969": q("polymarket", 0.295)}
    out = normalise_race(quotes, races)
    assert out["968"].value is None and out["970"].value == pytest.approx(0.705) and out["969"].value == pytest.approx(0.295)
    out = normalise_race({NH_D: q("polymarket", 0.855), NH_R: q("polymarket", 0.135)}, races)
    assert out[NH_D].value == pytest.approx(0.855 / 0.99) and out[NH_R].value == pytest.approx(0.135 / 0.99)
    out = normalise_race({NH_D: q("polymarket", 0.70), NH_R: q("polymarket", 0.40)}, races)
    assert out[NH_D].value is None and out[NH_R].value is None and "suspect-sum" in out[NH_D].flags
    out = normalise_race({NH_D: q("polymarket", 0.70)}, races)
    assert out[NH_D].value == 0.70  # one quoted leg keeps its raw value
    out = normalise_race({NH_D: q("polymarket", 0.70), NH_R: q("kalshi", 0.40)}, races)
    assert out[NH_D].value == 0.70 and out[NH_R].value == 0.40  # per venue


def test_blend_weights_agreement_and_uncertainty() -> None:
    race = fv._race_from_key(NH, "D")
    a, b = q("polymarket", 0.60, spread=0.01), q("kalshi", 0.62, spread=0.02)
    fair = blend(NH_D, [a, b], T0, race=race)
    wa, wb = 1 / 0.005 ** 2, 1 / 0.01 ** 2
    x = (wa * math.log(0.60 / 0.40) + wb * math.log(0.62 / 0.38)) / (wa + wb)
    assert fair.value == pytest.approx(1 / (1 + math.exp(-x)))
    assert fair.agreement == pytest.approx(0.02) and fair.confidence == "high" and fair.usable
    assert fair.source == "blend" and (fair.bid, fair.ask) == (a.bid, a.ask)
    assert fair.uncertainty == pytest.approx(0.02)  # max(agreement 0.02, half-spread 0.005, Kalshi fee band 0.02)
    assert fair.as_of == T0 and fair.race_key == NH and fair.party == "D"
    one = blend(NH_D, [q("polymarket", 0.60, spread=0.03)], T0)
    assert one.source == "polymarket" and one.agreement is None and one.confidence == "high"
    assert one.uncertainty == pytest.approx(0.015)
    assert one.value == 0.60
    lt = blend(NH_D, [q("polymarket", 0.60, spread=0.15, flags=["last-trade-only"])], T0)
    assert lt.confidence == "medium" and lt.uncertainty == pytest.approx(0.05)
    assert uncertainty_of([q("demo", 0.5, spread=0.01)], None) == pytest.approx(0.005)


def test_blend_confidence_levels() -> None:
    medium = blend("1", [q("polymarket", 0.60), q("kalshi", 0.64)], T0)
    assert medium.confidence == "medium" and medium.usable
    low = blend("1", [q("polymarket", 0.58), q("kalshi", 0.49)], T0)
    assert low.confidence == "low" and not low.usable
    assert low.reason == "Polymarket 0.58 and Kalshi 0.49 disagree by 9 cents: not traded on"
    wide = blend("1", [q("polymarket", 0.60, spread=0.08)], T0)
    assert wide.confidence == "medium"


def test_blend_staleness_at_90_s_and_match_threshold() -> None:
    fresh = blend("1", [q("polymarket", 0.6, fetched_at=T0 - 89)], T0)
    assert fresh.usable and fresh.as_of == T0 - 89
    stale = blend("1", [q("polymarket", 0.6, fetched_at=T0 - 91)], T0)
    assert stale.value is None and not stale.usable and "stale" in stale.sources[0].flags
    assert "91 s old" in stale.reason
    mixed = blend("1", [q("polymarket", 0.6, fetched_at=T0 - 91), q("kalshi", 0.62)], T0)
    assert mixed.value == pytest.approx(0.62) and mixed.source == "kalshi" and len(mixed.sources) == 2
    weak = blend("1", [q("polymarket", 0.6, conf=0.79)], T0)
    assert weak.value is None and "below 0.80" in weak.reason


def test_blend_near_matches_are_display_only_unless_trade_near() -> None:
    near = blend("842", [q("polymarket", 0.645, kind="NEAR", conf=0.8)], T0)
    assert near.value == pytest.approx(0.645) and not near.usable and near.match_kind == "NEAR"
    assert near.reason == "Near match (different settlement wording): shown, not traded"
    traded = blend("842", [q("polymarket", 0.645, kind="NEAR", conf=0.8)], T0, trade_near=True)
    assert traded.usable and traded.confidence == "medium"  # capped


def test_blend_suspect_gap_and_confirmed() -> None:
    suspect = blend("1", [q("polymarket", 0.86)], T0, cup_mid=0.42)
    assert suspect.suspect and not suspect.usable
    assert suspect.reason == ("Suspect match: outside 0.86 vs Cup 0.42; check the match (or confirm it in "
                              "fair_value_map.json)")
    ok = blend("1", [q("polymarket", 0.86)], T0, cup_mid=0.42, confirmed=True)
    assert not ok.suspect and ok.usable
    close = blend("1", [q("polymarket", 0.66)], T0, cup_mid=0.42)
    assert not close.suspect and close.usable


def test_combine_manual_wins_and_keeps_external_sources() -> None:
    ext = blend(NH_D, [q("polymarket", 0.86)], T0)
    manual = FairValue(exchange_id=NH_D, value=0.80, source="manual", usable=True, confidence="medium",
                       sources=[FairValueQuote(venue="manual", external_id="manual:1", value=0.8)], match_kind="MANUAL",
                       uncertainty=0.02)
    out = combine({NH_D: manual}, {NH_D: ext, NH_R: blend(NH_R, [q("polymarket", 0.14)], T0)})
    assert out[NH_D].source == "manual" and out[NH_D].value == 0.80
    assert [s.venue for s in out[NH_D].sources] == ["manual", "polymarket"]
    assert out[NH_D].agreement == pytest.approx(0.06) and out[NH_D].race_key == ext.race_key
    assert out[NH_R].source == "polymarket"


def test_renormalise_combined() -> None:
    races = races_for(infos_for(NH, "2026:SENATE:NE"))
    vals = {NH_D: FairValue(NH_D, 0.84, "polymarket", usable=True), NH_R: FairValue(NH_R, 0.18, "kalshi", usable=True)}
    out = renormalise_combined(vals, races)
    assert out[NH_D].value == pytest.approx(0.84 / 1.02) and out[NH_R].value == pytest.approx(0.18 / 1.02)
    assert vals[NH_D].value == 0.84  # inputs are not mutated
    bad = renormalise_combined({NH_D: FairValue(NH_D, 0.90, "polymarket", usable=True),
                                NH_R: FairValue(NH_R, 0.18, "kalshi", usable=True)}, races)
    assert not bad[NH_D].usable and not bad[NH_R].usable
    assert bad[NH_D].reason == "legs disagree: the race's fair values sum to 1.08"
    partial = renormalise_combined({"968": FairValue("968", 0.02, "polymarket", usable=True),
                                    "970": FairValue("970", 0.70, "polymarket", usable=True)}, races)
    assert partial["970"].value == 0.70  # the Independent leg has no value: the race is left as is
    mixed = renormalise_combined({NH_D: FairValue(NH_D, 0.80, "manual", usable=True),
                                  NH_R: FairValue(NH_R, 0.22, "polymarket", usable=True)}, races)
    assert mixed[NH_D].value == 0.80 and mixed[NH_R].value == pytest.approx(0.20)  # the user's own value is kept
    single = renormalise_combined({"9003": FairValue("9003", 0.6, "demo", usable=True)},
                                  {"9003": fv._race_from_key("2026:SENATE:MI", "D")})  # type: ignore[dict-item]
    assert single["9003"].value == 0.6 and single["9003"].usable  # a one-leg race is never "out of band"


# --------------------------------------------------------------------------- 8. service


def test_service_mode_off_reads_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "missing"
    provider = ScriptedProvider()
    svc = FairValueService(directory, mode="off", providers=[provider], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    snap = svc.refresh(T0)
    assert snap.enabled is False and snap.values == {} and snap.mode == "off"
    assert set(snap.races) == {NH_D, NH_R}  # races are still known (baskets)
    assert provider.calls == 0 and not directory.exists()
    st = svc.status()
    assert st["enabled"] is False and st["manual"] is None and st["map"] is None and st["providers"] == {}
    with pytest.raises(ValueError):
        FairValueService(directory, mode="sometimes")


def test_service_mode_manual_never_calls_providers(tmp_path: Path) -> None:
    (tmp_path / fv.MANUAL_JSON).write_text(json.dumps({"values": [{"exchange_id": NH_D, "probability": 0.6}]}), encoding="utf-8")
    provider = ScriptedProvider()
    provider.quotes[NH_D] = (0.5, 0.51)
    svc = FairValueService(tmp_path, mode="manual", providers=[provider], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    snap = svc.refresh(T0)
    assert provider.calls == 0
    assert snap.values[NH_D].value == 0.6 and snap.values[NH_D].source == "manual" and snap.values[NH_D].usable
    assert snap.values[NH_R].value is None and "Manual mode" in snap.values[NH_R].reason
    assert svc.status()["manual"]["entries"] == 1


def test_service_auto_with_fixture_providers(tmp_path: Path) -> None:
    transport = all_fixtures()
    clock = FakeClock()
    recorded: List[FairValueRecord] = []
    refreshes: List[FairValueRefresh] = []
    svc = FairValueService(tmp_path, providers=[pm_provider(transport, clock), ks_provider(transport, clock)], clock=clock,
                           recorder=recorded.extend, refresh_recorder=refreshes.append,
                           cup_mids=lambda: {NH_D: 0.84, NH_R: 0.15})
    before = svc.current()
    assert before.at is None and before.values == {} and set(before.races) == set()
    svc.set_targets(infos_for(NH, "2026:SENATE_CONTROL:US", "2026:GOVERNOR:AK"))
    assert set(svc.current().races) == {NH_D, NH_R, "842", "843", "1057", "1058"}
    snap = svc.refresh(T0)
    d = snap.values[NH_D]
    assert d.usable and d.source == "blend" and d.confidence == "high" and d.agreement == pytest.approx(0.0072, abs=1e-3)
    assert d.value == pytest.approx(0.86, abs=0.002) and d.uncertainty == pytest.approx(0.02)
    assert d.value + snap.values[NH_R].value == pytest.approx(1.0)
    assert {s.venue for s in d.sources} == {"polymarket", "kalshi"}
    assert not snap.values["842"].usable and snap.values["842"].match_kind == "NEAR"  # chamber control: shown only
    assert snap.values["1057"].reason == "No outside market prices this race by party (Alaska Governor)."
    st = svc.status()
    assert st["usable"] == 2 and st["total"] == 6 and st["last_refresh_at"] == T0
    assert st["providers"]["polymarket"]["status"] == "ok" and st["providers"]["polymarket"]["matched"] == 4
    assert st["providers"]["kalshi"]["quoted"] == 4 and st["providers"]["kalshi"]["last_ok_at"] == T0
    assert set(st["providers"]["polymarket"]) == {"status", "last_ok_at", "last_error", "requests", "matched", "quoted", "next_try_at"}
    assert st["map"]["path"].endswith(fv.MAP_FILE) and st["map"]["exists"] is True
    rows = {r.exchange_id: r for r in svc.matches()}
    assert [m.venue for m in rows[NH_D].matches] == ["kalshi", "polymarket"] and rows[NH_D].unmatched_reason is None
    assert rows["1057"].matches == [] and "Alaska Governor" in (rows["1057"].unmatched_reason or "")
    assert rows[NH_D].fair is not None and rows[NH_D].fair.value == d.value
    # the map file holds the validated matches, written atomically (no temp file left behind)
    doc = json.loads((tmp_path / fv.MAP_FILE).read_text())
    assert doc["version"] == 1 and doc["overrides"] == {} and doc["matches"][NH_D][0]["external_id"] in ("630844", "SENATENH-26-D")
    assert {m["venue"] for m in doc["matches"][NH_D]} == {"polymarket", "kalshi"}
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    # recording: one record per outcome, then a refresh row
    assert len(recorded) == 6 and {r.exchange_id for r in recorded} == {NH_D, NH_R, "842", "843", "1057", "1058"}
    rec = next(r for r in recorded if r.exchange_id == NH_D)
    assert rec.venues == ["kalshi", "polymarket"] and rec.as_of == T0 and rec.source == "blend"
    assert set(rec.detail) == {"as_of", "venues", "match_kind", "match_confidence", "uncertainty", "prev_value", "suspect"}
    assert refreshes[-1].ts == T0 and refreshes[-1].venues == {"polymarket": {"status": "ok", "fetched_at": T0},
                                                                "kalshi": {"status": "ok", "fetched_at": T0}}
    # prev_value at the next refresh
    clock.advance(60)
    snap2 = svc.refresh(T0 + 60)
    assert snap2.values[NH_D].prev_value == pytest.approx(d.value) and snap2.values[NH_D].prev_as_of == T0
    assert len(refreshes) == 2 and len(recorded) == 6  # nothing changed: no new records
    assert_get_only(transport)


def test_service_recorder_one_tick_changes_and_heartbeat(tmp_path: Path) -> None:
    provider = ScriptedProvider("demo")
    provider.quotes["9034"] = (0.985, 0.995)
    recorded: List[FairValueRecord] = []
    refreshes: List[FairValueRefresh] = []
    svc = FairValueService(tmp_path, providers=[provider], clock=FakeClock(), recorder=recorded.extend,
                           refresh_recorder=refreshes.append)
    svc.set_targets([ExchangeInfo("9034", "326", "YES", "Will the Maine Senate candidates debate before October 7?")])
    now = T0
    svc.refresh(now)
    assert len(recorded) == 1 and recorded[0].value == pytest.approx(0.99) and recorded[0].source == "demo"
    assert recorded[0].detail["uncertainty"] == pytest.approx(0.005) and recorded[0].usable
    for step in range(1, 10):  # 60 .. 540 s: unchanged or under one tick
        now = T0 + 60 * step
        provider.quotes["9034"] = (0.985 + (0.004 if step % 2 else 0.0), 0.995 + (0.004 if step % 2 else 0.0))
        svc.refresh(now)
    assert len(recorded) == 1
    svc.refresh(T0 + 600)  # heartbeat
    assert len(recorded) == 2 and recorded[-1].ts == T0 + 600
    provider.quotes["9034"] = (0.98, 0.99)  # value moved one tick (0.99 -> 0.985)
    svc.refresh(T0 + 660)
    assert len(recorded) == 3 and recorded[-1].value == pytest.approx(0.985)
    provider.fetched_offset = 120  # stale quote: usable flips -> recorded
    svc.refresh(T0 + 720)
    assert len(recorded) == 4 and recorded[-1].usable is False and recorded[-1].value is None
    assert len(refreshes) == 13 and refreshes[-1].venues["demo"]["status"] == "ok"


def test_service_editing_the_map_disables_an_outcome_within_one_refresh(tmp_path: Path) -> None:
    transport = all_fixtures()
    clock = FakeClock()
    svc = FairValueService(tmp_path, providers=[pm_provider(transport, clock)], clock=clock)
    svc.set_targets(infos_for(NH))
    assert svc.refresh(T0).values[NH_D].usable
    map_path = tmp_path / fv.MAP_FILE
    assert map_path.exists() and svc.status()["map"]["overrides"] == 0
    doc = json.loads(map_path.read_text())
    doc["overrides"] = {NH_D: {"disabled": True, "note": "wrong match"}}
    map_path.write_text(json.dumps(doc), encoding="utf-8")
    st = map_path.stat()
    os.utime(map_path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    snap = svc.refresh(T0 + 60)
    assert snap.values[NH_D].value is None and not snap.values[NH_D].usable and "disabled" in snap.values[NH_D].reason
    assert snap.values[NH_R].usable
    assert svc.status()["map"]["overrides"] == 1
    assert NH_D not in transport.calls[-1].url.params.get_list("id") and "630844" not in transport.calls[-1].url.params.get_list("id")
    # a later save of matches keeps the user's overrides verbatim
    svc.refresh(T0 + 120)
    assert json.loads(map_path.read_text())["overrides"] == {NH_D: {"disabled": True, "note": "wrong match"}}


def test_match_map_status_errors_and_never_overwrites_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / fv.MAP_FILE
    mm = MatchMap(path)
    assert mm.reload_if_changed() is False and mm.status()["exists"] is False and mm.status()["errors"] == []
    path.write_text(json.dumps({"version": 1, "overrides": {
        "1070": {"polymarket": 630844, "kalshi": "SENATENH-26-D", "race_key": "2026:SENATE:NH", "party": "D",
                 "confirmed": True, "note": "checked by hand", "colour": "blue"},
        "842": {"trade_near": "yes"},
        "843": "nope"}}), encoding="utf-8")
    assert mm.reload_if_changed() is True
    st = mm.status()
    assert st["exists"] and st["overrides"] == 2 and st["loaded_at"] is not None
    assert any("unknown key 'colour'" in e for e in st["errors"])
    assert any("trade_near must be true or false" in e for e in st["errors"])
    assert any("override 843: must be an object" in e for e in st["errors"])
    assert mm.overrides()["1070"]["polymarket"] == "630844" and "colour" not in mm.overrides()["1070"]
    assert mm.reload_if_changed() is False
    path.write_text("{broken", encoding="utf-8")
    st0 = path.stat()
    os.utime(path, ns=(st0.st_atime_ns, st0.st_mtime_ns + 2_000_000_000))
    assert mm.reload_if_changed() is True and mm.overrides() == {}
    assert any("could not be read" in e for e in mm.status()["errors"])
    mm.save_matches([fv.VenueMatch(venue="polymarket", exchange_id="1070", external_id="630844")], T0)
    assert path.read_text() == "{broken"  # the user's half-written edit is never overwritten
    assert any("not saved" in e for e in mm.status()["errors"])


def test_service_never_raises(tmp_path: Path) -> None:
    bad = ScriptedProvider("polymarket")
    bad.raise_error = RuntimeError("provider exploded")
    good = ScriptedProvider("kalshi")
    good.quotes[NH_D] = (0.85, 0.86)

    def boom(_records: Any) -> None:
        raise OSError("disk full")

    def mids() -> Dict[str, float]:
        raise RuntimeError("no view")

    svc = FairValueService(tmp_path, providers=[bad, good], clock=FakeClock(), recorder=boom, refresh_recorder=boom,
                           cup_mids=mids)
    svc.set_targets(infos_for(NH))
    snap = svc.refresh(T0)
    assert snap.values[NH_D].usable and snap.values[NH_D].source == "kalshi"
    assert svc.status()["providers"]["polymarket"]["status"] == "error"
    assert "provider failed (RuntimeError)" in svc.status()["providers"]["polymarket"]["last_error"]
    assert any("could not be recorded" in e for e in snap.errors)
    svc._manual = None  # type: ignore[assignment]  # break the service itself
    snap2 = svc.refresh(T0 + 60)
    assert any("refresh failed" in e for e in snap2.errors) and snap2.values[NH_D].usable  # the previous values are kept


def test_service_readonly_violation_propagates(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    provider.raise_error = ReadOnlyViolation("blocked")
    svc = FairValueService(tmp_path, providers=[provider], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    with pytest.raises(ReadOnlyViolation):
        svc.refresh(T0)


def test_service_disabled_targets_are_not_sent_to_providers(tmp_path: Path) -> None:
    write_map(tmp_path, {NH_D: {"disabled": True}})
    (tmp_path / fv.MANUAL_JSON).write_text(json.dumps({"values": [{"exchange_id": NH_D, "probability": 0.7}]}), encoding="utf-8")
    provider = ScriptedProvider()
    provider.quotes = {NH_D: (0.85, 0.86), NH_R: (0.13, 0.14)}
    svc = FairValueService(tmp_path, providers=[provider], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    snap = svc.refresh(T0)
    assert provider.seen[-1] == [NH_R]
    assert snap.values[NH_D].source == "manual" and snap.values[NH_D].value == 0.7  # the user's own value still applies


def test_service_suspect_gap_from_cup_mids_and_confirm(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    provider.quotes = {NH_D: (0.85, 0.86), NH_R: (0.13, 0.14)}
    svc = FairValueService(tmp_path, providers=[provider], clock=FakeClock(), cup_mids=lambda: {NH_D: 0.42, NH_R: 0.55})
    svc.set_targets(infos_for(NH))
    snap = svc.refresh(T0)
    assert snap.values[NH_D].suspect and not snap.values[NH_D].usable and "Suspect match" in snap.values[NH_D].reason
    write_map(tmp_path, {NH_D: {"confirmed": True}, NH_R: {"confirmed": True}}, bump=2)
    snap = svc.refresh(T0 + 60)
    assert not snap.values[NH_D].suspect and snap.values[NH_D].usable


def test_service_current_is_a_copy(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    provider.quotes = {NH_D: (0.85, 0.86)}
    svc = FairValueService(tmp_path, providers=[provider], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    svc.refresh(T0)
    snap = svc.current()
    snap.values[NH_D].value = 0.01
    snap.values[NH_D].sources.clear()
    snap.races.clear()
    again = svc.current()
    assert again.values[NH_D].value == pytest.approx(0.855) and again.values[NH_D].sources and again.races
    targets = svc.targets()
    targets[0].pins.clear()
    assert svc.targets()[0].pins


def test_service_demo_like_provider_and_no_race_outcome(tmp_path: Path) -> None:
    """The demo provider (§4.9) sends bid/ask/last only: the service computes the venue value; uncertainty 0.005."""

    class DemoLike:
        name = "demo"

        def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
            res = ProviderResult(venue="demo", status="ok")
            feed = {"9034": 0.99, "9026": 0.58, "9027": 0.42}
            for t in targets:
                if t.exchange_id in feed:
                    v = feed[t.exchange_id]
                    res.quotes[t.exchange_id] = FairValueQuote(
                        venue="demo", external_id=f"demo:{t.exchange_id}", match_kind="EXACT", match_confidence=1.0,
                        fetched_at=now, bid=round(v - 0.005, 3), ask=round(v + 0.005, 3), last=v)
            return res

        def close(self) -> None:
            pass

    svc = FairValueService(tmp_path, providers=[DemoLike()], clock=FakeClock())
    svc.set_targets([ExchangeInfo("9034", "326", "YES", "Will the Maine Senate candidates debate before October 7?"),
                     ExchangeInfo("9026", "318", "YES", "Will the Democratic Party win the Texas Senate?"),
                     ExchangeInfo("9027", "319", "YES", "Will the Republican Party win the Texas Senate?")])
    snap = svc.refresh(T0)
    for eid, value in (("9034", 0.99), ("9026", 0.58), ("9027", 0.42)):
        fair = snap.values[eid]
        assert fair.usable and fair.source == "demo" and fair.value == pytest.approx(value)
        assert fair.uncertainty == pytest.approx(0.005) and fair.confidence == "high"
    rows = {r.exchange_id: r for r in svc.matches()}
    assert rows["9034"].matches[0].source == "demo" and rows["9034"].unmatched_reason is None
    assert not (tmp_path / fv.MAP_FILE).exists()  # no validated matches: the map file is not written


def test_provider_limiter_is_never_the_super_market_limiter() -> None:
    client = SuperMarketClient("ace_fairvalue_test_key_0123456789",
                               transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    try:
        transport = all_fixtures()
        poly, kalshi = pm_provider(transport), ks_provider(transport)
        poly.refresh(targets_for(NH), T0)
        kalshi.refresh(targets_for(NH), T0)
        for provider, host in ((poly, GAMMA), (kalshi, "api.elections.kalshi.com")):
            limiter = provider.limiter(host)
            assert limiter is not client.read_limiter and limiter is not client.write_limiter
            assert limiter.limit == fv.EXTERNAL_READS_PER_MIN and limiter.used == 1
        assert poly.limiter(GAMMA) is not poly.limiter("clob.polymarket.com")
        assert client.read_limiter.used == 0 and client.requests_sent == 0
        assert all("ace_fairvalue_test_key" not in str(r.url) for r in transport.calls)
    finally:
        client.close()


def test_override_snippets() -> None:
    t = by_eid(targets_for(NH, "2026:SENATE_CONTROL:US"))
    assert override_snippet(t[NH_D], "disable") == '"1070": {"disabled": true, "note": "wrong match"}'
    assert override_snippet(t[NH_D], "pin") == '"1070": {"polymarket": "630844", "kalshi": "SENATENH-26-D", "confirmed": true}'
    assert override_snippet(t[NH_D], "pin", {"kalshi": "SENATENH-26-D"}) == '"1070": {"kalshi": "SENATENH-26-D", "confirmed": true}'
    assert override_snippet(t[NH_D], "confirm") == '"1070": {"confirmed": true}'
    assert override_snippet(t["842"], "trade_near") == '"842": {"trade_near": true}'
    nothing = MatchTarget(exchange_id="1057", market_id="368", title="x")
    assert '"confirmed": true' in override_snippet(nothing, "pin")
    with pytest.raises(ValueError):
        override_snippet(t[NH_D], "delete-everything")
    for action in ("disable", "pin", "confirm", "trade_near"):  # every snippet pastes into the overrides object
        text = override_snippet(t[NH_D], action)
        assert json.loads("{" + text + "}")["1070"]


def test_default_providers_and_caveats() -> None:
    providers = fv.default_providers("auto", transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert [p.name for p in providers] == ["polymarket", "kalshi"]
    for p in providers:
        p.close()
    assert fv.default_providers("manual") == [] and fv.default_providers("off") == []
    assert FV_CAVEATS == (  # exact texts, docs/PAPER_TRADING.md §10.4
        "Outside prices are other markets' opinions, not the truth: they carry their own biases (longshots tend to be "
        "overpriced) and fees.",
        "Polymarket resolves on media calls or certification and Kalshi on swearing-in; the Cup's own resolution rule "
        "may differ.",
        "Chamber control and Independent legs are near matches only: their settlement wording differs, so they are "
        "shown but not traded unless you enable them.",
        "A fair value is traded only when the gap exceeds its own uncertainty (venue disagreement, spread and fees) plus "
        "the minimum edge.",
    )


# --------------------------------------------------------------------------- 9. GET only


def test_outside_clients_are_get_only_and_anonymous() -> None:
    transport = all_fixtures()
    for provider in (pm_provider(transport), ks_provider(transport)):
        provider.refresh(targets_for(NH, "2026:SENATE:NE"), T0)
        client = provider._client
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(ReadOnlyViolation):
                client.request(method, "https://gamma-api.polymarket.com/markets")
        with pytest.raises(ReadOnlyViolation):
            client.get("https://gamma-api.polymarket.com/markets", headers={"Authorization": "Bearer x"})
    assert_get_only(transport)


# --------------------------------------------------------------------------- 10. history import (§4.11)


H_START, H_END = 1791126000.0, 1791129600.0  # history.json, Polymarket window
K_START, K_END = 1791122400.0, 1791133200.0  # history.json, Kalshi window


def _validated(provider: Any, transport: FixtureTransport, race_keys: Sequence[str] = (NH,)) -> List[MatchTarget]:
    targets = targets_for(*race_keys)
    provider.refresh(targets, T0)
    return targets


def test_history_polymarket_bar_end_and_indicative() -> None:
    transport = FixtureTransport("history.json", "polymarket_markets.json")
    provider = pm_provider(transport)
    targets = _validated(provider, transport)
    records, errors = provider.history(targets, H_START, H_END)
    assert errors == []
    d = sorted((r for r in records if r.exchange_id == NH_D), key=lambda r: r.ts)
    assert len(d) == 12 and d[0].ts == d[0].as_of == H_START + 300 and d[-1].ts == H_END  # bar END
    assert d[0].source == "history" and d[0].usable and d[0].confidence == "medium" and d[0].detail["indicative"] is True
    assert d[0].venues == ["polymarket"] and d[0].value == 0.85 and d[0].bid is None
    req = next(r for r in transport.calls if r.url.path == "/prices-history")
    assert req.url.host == "clob.polymarket.com" and req.url.params["fidelity"] == "5"
    assert req.url.params["startTs"] == str(int(H_START)) and req.url.params["endTs"] == str(int(H_END))
    assert_get_only(transport)


def test_history_kalshi_bid_ask_and_partial_bar_dropped() -> None:
    transport = FixtureTransport("history.json", "kalshi_markets.json")
    provider = ks_provider(transport)
    targets = _validated(provider, transport)
    records, errors = provider.history(targets, K_START, K_END)
    assert errors == []
    d = sorted((r for r in records if r.exchange_id == NH_D), key=lambda r: r.ts)
    assert [r.ts for r in d] == [1791126000.0, 1791129600.0, 1791133200.0]  # the bar ending after end_ts is dropped
    assert (d[0].bid, d[0].ask, d[0].value) == (0.85, 0.86, pytest.approx(0.855))
    assert d[0].detail["bid_ask"] is True and d[0].detail["indicative"] is False and d[0].as_of == d[0].ts
    req = next(r for r in transport.calls if r.url.path.endswith("/markets/candlesticks"))
    assert req.url.params["period_interval"] == "60"
    assert sorted(req.url.params["market_tickers"].split(",")) == ["SENATENH-26-D", "SENATENH-26-R"]
    assert_get_only(transport)


def test_import_history_normalises_each_race_and_records() -> None:
    transport = FixtureTransport("history.json", "polymarket_markets.json")
    provider = pm_provider(transport)
    targets = targets_for(NH)
    stored: List[FairValueRecord] = []
    out = import_history([provider], targets, races_for(infos_for(NH)), H_START, H_END, stored.extend)
    assert out["errors"] == [] and out["records"] == 24 and out["by_venue"] == {"polymarket": 24}
    assert out["start"] == H_START and out["end"] == H_END
    by_ts: Dict[float, Dict[str, FairValueRecord]] = {}
    for r in stored:
        by_ts.setdefault(r.ts, {})[r.exchange_id] = r
    first = by_ts[H_START + 300]
    assert first[NH_D].value + first[NH_R].value == pytest.approx(1.0)  # type: ignore[operator]
    assert first[NH_D].value == pytest.approx(0.85 / 0.99) and first[NH_D].detail["raw_value"] == 0.85
    # the window is clipped to HISTORY_MAX_DAYS
    clipped = import_history([], targets, {}, H_END - 30 * 86400, H_END, stored.extend)
    assert clipped["start"] == H_END - fv.HISTORY_MAX_DAYS * 86400
    assert_get_only(transport)


def test_import_history_offline_one_error_nothing_stored() -> None:
    transport = all_fixtures()
    transport.fail = lambda r: httpx.ProxyError("403 at CONNECT")
    stored: List[FairValueRecord] = []
    providers = [pm_provider(transport), ks_provider(transport)]
    infos = infos_for(NH, ME, "2026:SENATE:TX")
    targets = build_targets(infos, races_for(infos))
    out = import_history(providers, targets, races_for(infos), H_START, H_END, stored.extend)
    assert stored == [] and out["records"] == 0
    assert out["errors"] == [
        "Polymarket is unreachable from this machine (connection refused by the network proxy): no outside fair value from Polymarket.",
        "Kalshi is unreachable from this machine (connection refused by the network proxy): no outside fair value from Kalshi.",
    ]
    assert len(transport.calls) == 2  # one request per provider, then it stopped
    assert_get_only(transport)


# --------------------------------------------------------------------------- regressions (paper-trading verification)


class _SlowProvider:
    """A provider whose refresh takes ``takes`` seconds on the shared clock (Polymarket then Kalshi near the 30-s
    deadline); the outside price moves 0.50 -> 0.65 at ``jump_at``. ``ahead`` puts fetched_at that far after the
    service clock (a provider clock that runs ahead)."""

    name = "polymarket"

    def __init__(self, clock: FakeClock, takes: float, jump_at: float, ahead: float = 0.0) -> None:
        self.clock = clock
        self.takes = takes
        self.jump_at = jump_at
        self.ahead = ahead

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        self.clock.advance(self.takes)
        fetched = self.clock() + self.ahead
        price = 0.65 if fetched >= self.jump_at else 0.50
        res = ProviderResult(venue="polymarket", status="ok", requests=3)
        for t in targets:
            res.quotes[t.exchange_id] = FairValueQuote(
                venue="polymarket", external_id="pm:1", label="x", bid=price - 0.005, ask=price + 0.005, last=price,
                fetched_at=fetched, match_kind="EXACT", match_confidence=1.0)
        return res

    def close(self) -> None:
        pass


TX_INFO = ExchangeInfo("1", "m1", None, "Will the Democratic Party win the Texas Senate?")


def _slow_refresh(tmp_path: Path, *, explicit_now: bool, ahead: float = 0.0
                  ) -> Tuple[FairValueService, List[FairValueRecord], List[FairValueRefresh], FakeClock]:
    clock = FakeClock(T0)
    records: List[FairValueRecord] = []
    refreshes: List[FairValueRefresh] = []
    svc = FairValueService(tmp_path, providers=[_SlowProvider(clock, 40.0, T0 + 35.0, ahead)], clock=clock,
                           recorder=records.extend, refresh_recorder=refreshes.append)
    svc.set_targets([TX_INFO])
    svc.refresh(T0 if explicit_now else None)  # the tracker passes its clock's now explicitly
    return svc, records, refreshes, clock


@pytest.mark.parametrize("explicit_now", [True, False])
def test_lookahead_2_records_are_stamped_when_the_refresh_returns(tmp_path: Path, explicit_now: bool) -> None:
    svc, records, refreshes, clock = _slow_refresh(tmp_path, explicit_now=explicit_now)
    assert clock() == T0 + 40  # the providers took 40 s
    rec = records[-1]
    assert rec.value == pytest.approx(0.65) and rec.as_of == T0 + 40
    # available only from when the live service had it: when refresh() returned, never at its start
    assert rec.ts == T0 + 40 and rec.as_of <= rec.ts
    assert refreshes[-1].ts == T0 + 40 and refreshes[-1].venues["polymarket"] == {"status": "ok", "fetched_at": T0 + 40}
    assert svc.current().at == T0 + 40 and svc.status()["last_refresh_at"] == T0 + 40
    # the blend age still counts from the refresh time: a fresh quote is used
    assert svc.current().values["1"].usable


def test_lookahead_2_a_quote_fetched_after_the_measured_duration_bounds_the_stamp(tmp_path: Path) -> None:
    _svc, records, refreshes, _clock = _slow_refresh(tmp_path, explicit_now=True, ahead=5.0)
    assert records[-1].as_of == T0 + 45 and records[-1].ts == T0 + 45 and refreshes[-1].ts == T0 + 45


def test_lookahead_2_replay_never_trades_on_a_value_fetched_after_the_decision(tmp_path: Path,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the record of a 40-s refresh replayed with latency 30 s (the --latency-sweep's first row). The
    Cup ask is 0.52 until T0+75, then 0.66; a value buyer (fv - ask >= 0.05, limit 0.55) could not buy at 0.52
    live (0.65 existed from T0+40; the next decision T0+60 fills at >= T0+90). Before the fix the replay used fv
    0.65 (as_of T0+40) at the T0+30 decision and booked +130."""
    from supermarket_bot import backtest as B
    from supermarket_bot import sizing
    from supermarket_bot.models import (
        BacktestConfig, BetShape, BookObservation, ExitPlan, Opportunity, PaperConfig, PortfolioSpec, PricePoint,
        SizeDecision,
    )

    _svc, records, refreshes, _clock = _slow_refresh(tmp_path, explicit_now=True)

    class Fixed:
        name = "fixed"
        label = "Fixed units"

        def size(self, opp: Any, ctx: Any) -> Any:
            return SizeDecision(idea_id=str(opp.idea_id), policy=self.name, units=1000, stake=0.0)

        def size_all(self, opps: Sequence[Any], ctx: Any) -> Dict[str, Any]:
            return {str(o.idea_id): self.size(o, ctx) for o in opps}

        def explain(self, ctx: Any) -> Dict[str, Any]:
            return {"policy": self.name, "label": self.label, "mode": None, "lines": []}

    monkeypatch.setattr(sizing, "get_policy", lambda name: Fixed())
    pts: List[PricePoint] = []
    books: List[BookObservation] = []
    t = T0 - 7 * 3600
    while t <= T0 + 1800:
        a = 0.52 if t < T0 + 75 else 0.66
        pts.append(PricePoint(ts=t, price=a - 0.005, last=None, bid=round(a - 0.01, 3), ask=a, source="tick"))
        books.append(BookObservation("1", t, bids=[(round(a - 0.01, 3), 5000)], asks=[(a, 5000)], source="paper"))
        t += 15.0
    hist = B.MemoryHistory(exchanges=[TX_INFO], points={"1": pts}, books={"1": books}, fair_values=records,
                           refreshes=refreshes)
    seen: List[Tuple[float, float, float]] = []
    future: List[Tuple[float, float]] = []

    def signal_fn(inputs: Any, params: Any) -> List[Any]:
        fair = inputs.fair_values.get("1")
        q = inputs.latest.get("1")
        if fair is None or fair.value is None or q is None or q.ask is None:
            return []
        if fair.as_of is not None and fair.as_of > inputs.now:  # a value from the decision's future
            future.append((inputs.now, fair.as_of))
        if fair.value - q.ask >= 0.05:
            seen.append((inputs.now, fair.value, fair.as_of))
            return [Opportunity(
                kind="value", exchange_id="1", market_id="m1", title=TX_INFO.market_title, option=None, side="yes",
                entry_price=q.ask, target_price=None, stop_price=None, prob_win=0.6, edge=0.05, expected_return=0.1,
                horizon_hours=1.0, suggested_shares=0, suggested_cost=0.0, score=1.0, confidence=0.6,
                rationale=["value"], idea_id="value:x1:yes", race_key=None, bet=BetShape("binary", 0.6, 0.4, 0.5, q.ask),
                exit_plan=ExitPlan(kind="value"), order_type="taker", limit_price=0.55, expires_at=None,
                fair_value=fair.value, factor_delta=0.05)]
        return []

    for lat in (30.0, 60.0):
        seen.clear()
        future.clear()
        cfg = BacktestConfig(start=T0, end=T0 + 1800, hours=0.5, step_s=30.0, latency_s=lat, use_fair_values=True,
                             paper=PaperConfig(portfolios=[PortfolioSpec("bot", "bot", "fixed", None, None, 30.0, 120.0)]))
        rep = B.run_backtest(hist, cfg, signal_fn=signal_fn)
        s = rep.portfolios[0]
        assert future == [], (lat, future)  # the engine swallows exceptions in signal_fn: checked here
        assert seen == [] and s.fills == 0 and s.pnl_liq == pytest.approx(0.0), (lat, seen, s.fills, s.pnl_liq)


def test_live_4_offline_venues_never_show_a_last_answer_and_keep_their_cause(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    tr = all_fixtures()
    tr.fail = lambda r: httpx.ProxyError("403 Forbidden")  # every CONNECT refused, like the sandbox proxy
    providers = [pm_provider(tr, clock), ks_provider(tr, clock)]
    svc = FairValueService(tmp_path, providers=providers, clock=clock)
    # the tracker's fair-value worker refreshes before the first market list is read: no targets yet
    svc.refresh(T0)
    st = svc.status()["providers"]
    assert tr.calls == []
    for name in ("polymarket", "kalshi"):
        assert st[name]["status"] == "pending" and st[name]["last_ok_at"] is None, st[name]
    svc.set_targets(infos_for(NH))
    seen = []
    for k in range(1, 61):  # one hour of 60-s refreshes, all refused
        clock.t = T0 + 60 * k
        svc.refresh(clock.t)
        st = svc.status()["providers"]
        for name, label in (("polymarket", "Polymarket"), ("kalshi", "Kalshi")):
            p = st[name]
            seen.append(p["status"])
            assert p["last_ok_at"] is None, (k, name, p)  # never answered: no "last answer"
            assert p["status"] == "offline", (k, name, p)  # offline from this machine, every refresh
            assert p["last_error"].startswith(f"{label} is unreachable from this machine"), (k, name, p)
    assert set(seen) == {"offline"} and 0 < len(tr.calls) < 60  # still backing off: few requests were sent
    # the venues come back: the first real answer sets last_ok_at
    tr.fail = None
    nxt = max(p["next_try_at"] for p in svc.status()["providers"].values())
    clock.t = nxt
    svc.refresh(clock.t)
    st = svc.status()["providers"]
    assert st["polymarket"]["status"] == "ok" and st["polymarket"]["last_ok_at"] == nxt
    assert st["polymarket"]["last_error"] is None


def test_live_4_backoff_after_a_server_error_keeps_the_cause_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    clock = FakeClock()
    provider = PolymarketProvider(transport=httpx.MockTransport(handler), clock=clock, sleep=clock.advance)
    res = provider.refresh(targets_for(NH), T0)
    assert res.status == "error" and "server error (HTTP 503)" in res.errors[0]
    during = provider.refresh(targets_for(NH), T0 + 1)
    assert during.status == "backoff" and during.requests == 0
    assert during.errors[0] == res.errors[0] and "backing off after an error: next try at" in during.errors[1]


def test_live_4_a_refresh_that_asked_nothing_is_not_an_answer(tmp_path: Path) -> None:
    quiet = ScriptedProvider("kalshi")  # status "ok", no request, no quote for this outcome
    talks = ScriptedProvider("polymarket")
    talks.quotes[NH_D] = (0.85, 0.86)
    svc = FairValueService(tmp_path, providers=[talks, quiet], clock=FakeClock())
    svc.set_targets(infos_for(NH))
    svc.refresh(T0)
    st = svc.status()["providers"]
    assert st["kalshi"]["status"] == "pending" and st["kalshi"]["last_ok_at"] is None
    assert st["polymarket"]["status"] == "ok" and st["polymarket"]["last_ok_at"] == T0  # quotes are an answer
