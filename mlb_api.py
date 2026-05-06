"""
MLB Stats API client — fetches game data and computes pitcher state.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
LIVE_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

VALID_GAME_TYPES = {"R", "P", "F", "D", "L", "W"}  # regular, playoffs; exclude 'S' spring training, 'E' exhibition


@dataclass
class PitcherLine:
    pitcher_id: int
    name: str
    team_abbrev: str
    opponent_abbrev: str
    is_home: bool  # True = SP's team is home team

    # Pitching stats (from boxscore)
    ip: float          # innings pitched as decimal (6.1 = 6⅓)
    ip_str: str        # display string like "6.1"
    hits: int
    earned_runs: int
    walks: int
    strikeouts: int
    pitch_count: int

    # Context for the final post
    is_complete_game: bool
    removal_inning: int           # inning number when removed (1-indexed)
    removal_half: str             # "top" or "bottom"
    responsible_runners_scored: int   # SP-responsible runners that scored
    responsible_runners_lob: int      # still on base when inning ended (LOB)
    responsible_runners_outstanding: int  # still on base, inning still in progress

    game_pk: int


@dataclass
class GamePitcherState:
    """Tracks live per-game pitcher state needed to determine when a line is final."""
    game_pk: int
    game_status: str   # "In Progress", "Final", etc.
    game_type: str

    # Keyed by "home" and "away"
    starting_pitcher: dict = field(default_factory=dict)   # {side: {id, name, ...}}
    current_pitcher: dict = field(default_factory=dict)    # {side: pitcher_id}

    # When the SP was removed: inning + half + which runner IDs were on base
    removal_info: dict = field(default_factory=dict)       # {side: {inning, half, runner_ids: set}}

    # Runner tracking: runner_id -> {base, responsible_pitcher_id}
    runners: dict = field(default_factory=dict)

    # Current linescore
    current_inning: int = 0
    current_half: str = "top"   # "top" or "bottom"
    current_outs: int = 0

    # Home/away team abbreviations
    home_team: str = ""
    away_team: str = ""


def fetch_schedule(date_str: str) -> list[dict]:
    """Return list of game dicts for the given date (YYYY-MM-DD)."""
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "linescore,boxscore,pitchers,team",
    }
    try:
        r = requests.get(SCHEDULE_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.error("Schedule fetch failed: %s", exc)
        return []

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append(g)
    return games


def fetch_live_feed(game_pk: int) -> Optional[dict]:
    """Return the full live feed JSON for a game, or None on error."""
    url = LIVE_FEED_URL.format(gamePk=game_pk)
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error("Live feed fetch failed for gamePk=%s: %s", game_pk, exc)
        return None


def _outs_to_ip_str(outs: int) -> tuple[float, str]:
    """Convert total outs recorded to IP float and display string."""
    full_innings = outs // 3
    partial = outs % 3
    ip_float = full_innings + partial / 10  # 6.1 = 6⅓
    ip_str = f"{full_innings}.{partial}" if partial else str(full_innings)
    return ip_float, ip_str


def _side_for_pitcher(pitcher_id: int, boxscore: dict) -> Optional[str]:
    """Return 'home' or 'away' for which team this pitcher belongs to."""
    for side in ("home", "away"):
        team_data = boxscore.get("teams", {}).get(side, {})
        pitchers = team_data.get("pitchers", [])
        if pitcher_id in pitchers:
            return side
    return None


def _get_pitcher_stats(pitcher_id: int, side: str, boxscore: dict) -> Optional[dict]:
    """Return the pitching stat line dict for a specific pitcher from the boxscore."""
    team_data = boxscore.get("teams", {}).get(side, {})
    players = team_data.get("players", {})
    key = f"ID{pitcher_id}"
    player = players.get(key, {})
    return player.get("stats", {}).get("pitching", None)


def _get_starting_pitcher_info(side: str, boxscore: dict, all_players: dict) -> Optional[dict]:
    """
    Return {id, name} for the starting pitcher on a given side.
    The first pitcher listed in boxscore teams[side].pitchers is the starter.
    """
    team_data = boxscore.get("teams", {}).get(side, {})
    pitcher_ids = team_data.get("pitchers", [])
    if not pitcher_ids:
        return None
    starter_id = pitcher_ids[0]
    key = f"ID{starter_id}"
    player_data = team_data.get("players", {}).get(key, {})
    name = player_data.get("person", {}).get("fullName", f"Pitcher {starter_id}")
    return {"id": starter_id, "name": name}


def _current_pitcher_id(side: str, linescore: dict) -> Optional[int]:
    """Return the ID of the current pitcher on the mound for a given side."""
    # linescore.teams.[home|away].pitcher
    team_ls = linescore.get("teams", {}).get(side, {})
    pitcher = team_ls.get("pitcher", {})
    return pitcher.get("id")


def _parse_runners(live_data: dict) -> dict:
    """
    Parse current base occupants from linescore.offense.
    Returns {runner_id: base_label} e.g. {12345: "first", 67890: "second"}
    """
    offense = live_data.get("linescore", {}).get("offense", {})
    runners = {}
    for base in ("first", "second", "third"):
        runner = offense.get(base)
        if runner:
            runners[runner["id"]] = base
    return runners


def _team_abbrev(side: str, game_data: dict) -> str:
    teams = game_data.get("gameData", {}).get("teams", {})
    return teams.get(side, {}).get("abbreviation", side.upper())


def parse_game_state(game_pk: int, live_data: dict, prev_state: Optional[GamePitcherState]) -> GamePitcherState:
    """
    Build a GamePitcherState from the live feed.
    Carries over runner-responsibility tracking from prev_state.
    """
    game_data = live_data.get("gameData", {})
    live = live_data.get("liveData", {})
    boxscore = live.get("boxscore", {})
    linescore = live.get("linescore", {})

    status = game_data.get("status", {}).get("detailedState", "")
    game_type = game_data.get("game", {}).get("type", "")

    home_abbrev = _team_abbrev("home", live_data)
    away_abbrev = _team_abbrev("away", live_data)

    current_inning = linescore.get("currentInning", 0)
    current_half_raw = linescore.get("inningHalf", "Top")
    current_half = current_half_raw.lower()  # "top" or "bottom"
    current_outs = linescore.get("outs", 0)

    state = GamePitcherState(
        game_pk=game_pk,
        game_status=status,
        game_type=game_type,
        home_team=home_abbrev,
        away_team=away_abbrev,
        current_inning=current_inning,
        current_half=current_half,
        current_outs=current_outs,
    )

    # Carry over runner tracking from previous state
    if prev_state:
        state.removal_info = prev_state.removal_info.copy()
        # We'll rebuild runners below from live data

    for side in ("home", "away"):
        sp_info = _get_starting_pitcher_info(side, boxscore, {})
        if sp_info:
            state.starting_pitcher[side] = sp_info

        current_id = _current_pitcher_id(side, linescore)
        if current_id:
            state.current_pitcher[side] = current_id

        # Detect SP removal: SP was pitching last tick, no longer current pitcher
        sp_id = state.starting_pitcher.get(side, {}).get("id")
        if sp_id and sp_id != current_id and side not in state.removal_info:
            # SP has been removed — record the inning/half and current runners on base
            # The "responsible" runners are whoever is currently on base for the pitching side
            # (runners the SP put on who the reliever inherited)
            current_runners_on = _parse_runners(live_data)
            # Filter to runners the SP put on — we assume any runner currently on base
            # at the moment of removal is the SP's responsibility (standard baseball scoring)
            state.removal_info[side] = {
                "inning": current_inning,
                "half": current_half,
                "runner_ids": set(current_runners_on.keys()),
            }
            log.info(
                "SP removed: %s (%s) — inning %s %s, %d runners inherited",
                sp_info.get("name"),
                side,
                current_half,
                current_inning,
                len(current_runners_on),
            )

    # Update runner tracking: carry forward, merging with live state
    live_runners = _parse_runners(live_data)
    state.runners = live_runners

    return state


def _is_sp_line_final(side: str, state: GamePitcherState) -> bool:
    """
    Return True if the starting pitcher's line is considered final.
    See spec for logic.
    """
    sp_id = state.starting_pitcher.get(side, {}).get("id")
    if not sp_id:
        return False

    game_over = state.game_status in ("Final", "Game Over", "Completed Early")

    current_pitcher_id = state.current_pitcher.get(side)
    sp_still_pitching = (current_pitcher_id == sp_id)

    # Complete game: SP is still pitching when game ends
    if sp_still_pitching and game_over:
        return True

    # SP hasn't been removed yet and game isn't over
    if sp_still_pitching:
        return False

    # SP was never set as current pitcher (game data gap) — not final
    if side not in state.removal_info:
        return False

    removal = state.removal_info[side]
    responsible_runner_ids = removal["runner_ids"]

    # No runners inherited — line is immediately final
    if not responsible_runner_ids:
        return True

    # Check if inning has advanced past the removal inning
    removal_inning = removal["inning"]
    removal_half = removal["half"]

    inning_ended = (
        state.current_inning > removal_inning
        or (state.current_inning == removal_inning and state.current_half != removal_half)
    ) or game_over

    if inning_ended:
        return True

    # Inning still in progress — check if all responsible runners are gone from bases
    still_on_base = responsible_runner_ids & set(state.runners.keys())
    if not still_on_base:
        return True

    return False


def _count_responsible_runner_fates(side: str, state: GamePitcherState, boxscore: dict) -> tuple[int, int, int]:
    """
    Returns (scored, lob, outstanding) for SP-responsible runners.
    - scored: inherited runners that scored (charged to SP as ER)
    - lob: inherited runners left on base when inning ended
    - outstanding: still on base, inning in progress
    """
    if side not in state.removal_info:
        return 0, 0, 0

    responsible_ids = state.removal_info[side]["runner_ids"]
    if not responsible_ids:
        return 0, 0, 0

    still_on = responsible_ids & set(state.runners.keys())
    gone = responsible_ids - still_on

    removal_inning = state.removal_info[side]["inning"]
    removal_half = state.removal_info[side]["half"]

    inning_over = (
        state.current_inning > removal_inning
        or (state.current_inning == removal_inning and state.current_half != removal_half)
        or state.game_status in ("Final", "Game Over", "Completed Early")
    )

    outstanding = len(still_on) if not inning_over else 0
    lob = len(still_on) if inning_over else 0

    # "gone" runners either scored or were put out — we can't easily distinguish
    # without parsing play-by-play. Use a best-effort: if ER increased after removal,
    # attribute those to the SP. For simplicity we'll count "gone" as scored for display
    # purposes (the boxscore ER stat already handles actual scoring).
    scored = len(gone)

    return scored, lob, outstanding


def build_pitcher_line(side: str, state: GamePitcherState, live_data: dict) -> Optional[PitcherLine]:
    """
    Build a PitcherLine from live state. Returns None if data is incomplete.
    """
    sp_info = state.starting_pitcher.get(side)
    if not sp_info:
        return None

    sp_id = sp_info["id"]
    live = live_data.get("liveData", {})
    boxscore = live.get("boxscore", {})

    stats = _get_pitcher_stats(sp_id, side, boxscore)
    if not stats:
        log.debug("No pitching stats found for pitcher %s", sp_id)
        return None

    outs_pitched = stats.get("outs", 0)
    ip_float, ip_str = _outs_to_ip_str(outs_pitched)

    opponent_side = "home" if side == "away" else "away"
    team_abbrev = state.home_team if side == "home" else state.away_team
    opp_abbrev = state.away_team if side == "home" else state.home_team

    current_pitcher_id = state.current_pitcher.get(side)
    is_cg = (current_pitcher_id == sp_id) and state.game_status in ("Final", "Game Over", "Completed Early")

    removal = state.removal_info.get(side, {})
    removal_inning = removal.get("inning", 0)
    removal_half = removal.get("half", "top")

    scored, lob, outstanding = _count_responsible_runner_fates(side, state, boxscore)

    return PitcherLine(
        pitcher_id=sp_id,
        name=sp_info["name"],
        team_abbrev=team_abbrev,
        opponent_abbrev=opp_abbrev,
        is_home=(side == "home"),
        ip=ip_float,
        ip_str=ip_str,
        hits=stats.get("hits", 0),
        earned_runs=stats.get("earnedRuns", 0),
        walks=stats.get("baseOnBalls", 0),
        strikeouts=stats.get("strikeOuts", 0),
        pitch_count=stats.get("numberOfPitches", 0),
        is_complete_game=is_cg,
        removal_inning=removal_inning,
        removal_half=removal_half,
        responsible_runners_scored=scored,
        responsible_runners_lob=lob,
        responsible_runners_outstanding=outstanding,
        game_pk=state.game_pk,
    )


def format_post(line: PitcherLine) -> str:
    """Format a PitcherLine into the Bluesky post string."""
    stat_line = (
        f"{line.ip_str} IP {line.hits} H {line.earned_runs} ER "
        f"{line.walks} BB {line.strikeouts} K {line.pitch_count} pitches"
    )

    at_symbol = "@"
    # "SP's team @ opponent"
    matchup = f"({line.team_abbrev}) {at_symbol}{line.opponent_abbrev}"

    if line.is_complete_game:
        context = "Threw a complete game."
    else:
        half_label = "top" if line.removal_half == "top" else "bottom"
        ordinal = _ordinal(line.removal_inning)

        total_responsible = (
            line.responsible_runners_scored
            + line.responsible_runners_lob
            + line.responsible_runners_outstanding
        )

        if total_responsible == 0:
            context = f"Left in {half_label} of {ordinal} with no runners on."
        elif line.responsible_runners_lob > 0 and line.responsible_runners_scored == 0:
            runner_word = "runner" if total_responsible == 1 else "runners"
            context = f"Left in {half_label} of {ordinal} with {total_responsible} {runner_word} on. Inning ended with runners LOB."
        else:
            runner_word = "runner" if total_responsible == 1 else "runners"
            s = line.responsible_runners_scored
            lob = line.responsible_runners_lob
            if s == total_responsible == 1:
                scored_note = " (scored)"
            elif s == total_responsible:
                scored_note = " (both scored)" if total_responsible == 2 else " (all scored)"
            elif lob > 0:
                scored_note = f" ({s} scored, {lob} LOB)"
            else:
                scored_note = f" ({s} scored)"
            context = f"Left in {half_label} of {ordinal} with {total_responsible} {runner_word} on{scored_note}."

    return f"{line.name} {matchup}: {stat_line}. {context}"


def _ordinal(n: int) -> str:
    suffixes = {1: "1st", 2: "2nd", 3: "3rd"}
    return suffixes.get(n, f"{n}th")


# ---------------------------------------------------------------------------
# Quick smoke-test: fetch today's schedule and print game PKs + status
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from datetime import date

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    test_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print(f"\nFetching schedule for {test_date}...")
    games = fetch_schedule(test_date)
    print(f"Found {len(games)} total games\n")

    for g in games:
        pk = g.get("gamePk")
        status = g.get("status", {}).get("detailedState", "?")
        gtype = g.get("gameType", "?")
        away = g.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "?")
        home = g.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "?")
        print(f"  gamePk={pk}  type={gtype}  status={status}  {away}@{home}")

    # Pick first valid R/P game and inspect its live feed
    valid = [g for g in games if g.get("gameType") in VALID_GAME_TYPES]
    if not valid:
        print("\nNo valid regular/postseason games found for this date.")
        sys.exit(0)

    sample = valid[0]
    pk = sample["gamePk"]
    print(f"\nFetching live feed for gamePk={pk}...")
    live = fetch_live_feed(pk)
    if not live:
        print("Failed to fetch live feed.")
        sys.exit(1)

    game_data = live.get("gameData", {})
    liveData = live.get("liveData", {})
    boxscore = liveData.get("boxscore", {})
    linescore = liveData.get("linescore", {})

    print(f"\nGame status : {game_data.get('status', {}).get('detailedState')}")
    print(f"Current inning: {linescore.get('currentInning')} {linescore.get('inningHalf')}")
    print(f"Outs: {linescore.get('outs')}")

    for side in ("away", "home"):
        team_data = boxscore.get("teams", {}).get(side, {})
        pitcher_ids = team_data.get("pitchers", [])
        if pitcher_ids:
            sp_id = pitcher_ids[0]
            sp_key = f"ID{sp_id}"
            sp_player = team_data.get("players", {}).get(sp_key, {})
            sp_name = sp_player.get("person", {}).get("fullName", "?")
            sp_stats = sp_player.get("stats", {}).get("pitching", {})
            outs = sp_stats.get("outs", 0)
            ip_float, ip_str = _outs_to_ip_str(outs)
            print(f"\n  [{side.upper()}] SP: {sp_name} (ID {sp_id})")
            print(f"    IP={ip_str}  H={sp_stats.get('hits')}  ER={sp_stats.get('earnedRuns')}  "
                  f"BB={sp_stats.get('baseOnBalls')}  K={sp_stats.get('strikeOuts')}  "
                  f"Pitches={sp_stats.get('numberOfPitches')}")

    print("\nOffense (runners on base):")
    offense = linescore.get("offense", {})
    for base in ("first", "second", "third"):
        r = offense.get(base)
        if r:
            print(f"  {base}: {r.get('fullName')} (ID {r.get('id')})")
        else:
            print(f"  {base}: empty")
