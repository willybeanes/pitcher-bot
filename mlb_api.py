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
    team_name: str        # nickname, e.g. "Rangers"
    opponent_abbrev: str
    is_home: bool         # True = SP's team is home team

    # Pitching stats (from boxscore)
    ip: float          # innings pitched as decimal (6.1 = 6⅓)
    ip_str: str        # display string like "6.1"
    hits: int
    earned_runs: int
    walks: int
    strikeouts: int
    pitch_count: int

    # Score when SP exited (or final score for CG)
    sp_score: int
    opp_score: int

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

    # Home/away team abbreviations and nicknames
    home_team: str = ""
    away_team: str = ""
    home_team_name: str = ""   # e.g. "Tigers"
    away_team_name: str = ""   # e.g. "Red Sox"


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


def _parse_sp_removal_from_plays(sp_id: int, side: str, all_plays: list) -> Optional[dict]:
    """
    Parse play-by-play to find when the SP was removed, the score at that moment,
    and the fate of any inherited runners.

    Returns a removal_info dict, or None if the SP is still pitching / hasn't pitched.
    """
    # Find the last at-bat the SP pitched
    last_sp_idx = None
    for i, play in enumerate(all_plays):
        if play.get("matchup", {}).get("pitcher", {}).get("id") == sp_id:
            last_sp_idx = i

    if last_sp_idx is None:
        return None  # SP hasn't pitched yet

    last_sp_play = all_plays[last_sp_idx]
    inning = last_sp_play["about"]["inning"]
    half = last_sp_play["about"]["halfInning"]  # "top" or "bottom"

    # Home SPs pitch in top half-innings (facing away batters).
    # Away SPs pitch in bottom half-innings (facing home batters).
    # Only plays in the SP's own pitching half count as evidence of removal.
    # If we see no subsequent plays in that half, they simply haven't taken
    # the mound for the next inning yet — don't mistake this for removal.
    sp_half = "top" if side == "home" else "bottom"
    subsequent_same_half = [
        p for p in all_plays[last_sp_idx + 1 :]
        if p["about"]["halfInning"] == sp_half and p["about"].get("isComplete", False)
    ]

    if not subsequent_same_half:
        return None  # No evidence of removal — SP will pitch (or is pitching) next

    # If the first subsequent same-half play is still the SP, they haven't been removed
    if subsequent_same_half[0]["matchup"]["pitcher"].get("id") == sp_id:
        return None

    # Score from the SP's last at-bat result
    result = last_sp_play["result"]
    if side == "away":
        sp_score = result.get("awayScore", 0)
        opp_score = result.get("homeScore", 0)
    else:
        sp_score = result.get("homeScore", 0)
        opp_score = result.get("awayScore", 0)

    # Simulate base state through all plays up to and including SP's last.
    # Reset at each half-inning boundary — runners LOB don't get explicit "out"
    # events in the play-by-play, so without resetting we'd carry phantom runners.
    bases: dict[int, str] = {}
    current_half_key: tuple | None = None
    for play in all_plays[: last_sp_idx + 1]:
        half_key = (play["about"]["inning"], play["about"]["halfInning"])
        if half_key != current_half_key:
            bases = {}
            current_half_key = half_key
        for entry in play.get("runners", []):
            movement = entry.get("movement", {})
            details = entry.get("details", {})
            runner_id = details.get("runner", {}).get("id")
            if not runner_id:
                continue
            end_base = movement.get("end")
            if movement.get("isOut") or end_base in (None, "score"):
                bases.pop(runner_id, None)
            elif end_base in ("1B", "2B", "3B"):
                bases[runner_id] = end_base

    # If there are no subsequent plays in the same half-inning, the SP completed
    # the inning cleanly (was removed between innings). Any runners in bases are
    # their own LOB — not inherited by the next pitcher.
    same_inning_subsequent = [
        p for p in all_plays[last_sp_idx + 1:]
        if p["about"]["inning"] == inning and p["about"]["halfInning"] == half
    ]
    if not same_inning_subsequent:
        inherited_ids = set()
    else:
        inherited_ids = set(bases.keys())

    # Track outcomes for inherited runners in subsequent plays of the same half-inning
    runners_scored = 0
    accounted: set[int] = set()

    for play in all_plays[last_sp_idx + 1 :]:
        if play["about"]["inning"] != inning or play["about"]["halfInning"] != half:
            break
        for entry in play.get("runners", []):
            movement = entry.get("movement", {})
            details = entry.get("details", {})
            runner_id = details.get("runner", {}).get("id")
            if runner_id not in inherited_ids or runner_id in accounted:
                continue
            if details.get("isScoringEvent"):
                runners_scored += 1
                accounted.add(runner_id)
            elif movement.get("isOut"):
                accounted.add(runner_id)  # put out — counts as LOB

    # Determine if the removal inning has definitively ended by checking whether
    # any later play belongs to a different half-inning.
    inning_ended = any(
        p["about"]["inning"] > inning
        or (p["about"]["inning"] == inning and p["about"]["halfInning"] != half)
        for p in all_plays[last_sp_idx + 1 :]
    )
    outstanding = set() if inning_ended else (inherited_ids - accounted)
    runners_lob = len(inherited_ids) - runners_scored - len(outstanding)

    log.info(
        "SP removed (plays): %s (%s) — %s of inning %s, score %s-%s, "
        "%d inherited (%d scored, %d LOB, %d outstanding)",
        sp_id, side, half, inning, sp_score, opp_score,
        len(inherited_ids), runners_scored, runners_lob, len(outstanding),
    )

    return {
        "inning": inning,
        "half": half,
        "sp_score": sp_score,
        "opp_score": opp_score,
        "runner_ids": inherited_ids,
        "runners_scored": runners_scored,
        "runners_lob": runners_lob,
        "runners_outstanding": outstanding,
    }


def _team_abbrev(side: str, game_data: dict) -> str:
    teams = game_data.get("gameData", {}).get("teams", {})
    return teams.get(side, {}).get("abbreviation", side.upper())


def _team_name(side: str, game_data: dict) -> str:
    """Return team nickname (e.g. 'Rangers', 'Red Sox')."""
    teams = game_data.get("gameData", {}).get("teams", {})
    return teams.get(side, {}).get("teamName", side.upper())


def _linescore_runs(side: str, linescore: dict) -> int:
    return linescore.get("teams", {}).get(side, {}).get("runs", 0)


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
    home_name = _team_name("home", live_data)
    away_name = _team_name("away", live_data)

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
        home_team_name=home_name,
        away_team_name=away_name,
        current_inning=current_inning,
        current_half=current_half,
        current_outs=current_outs,
    )

    all_plays = live.get("plays", {}).get("allPlays", [])

    for side in ("home", "away"):
        sp_info = _get_starting_pitcher_info(side, boxscore, {})
        if sp_info:
            state.starting_pitcher[side] = sp_info

        current_id = _current_pitcher_id(side, linescore)
        if current_id:
            state.current_pitcher[side] = current_id

        # Use play-by-play to determine removal details — accurate for both
        # live games (plays so far) and completed games.
        sp_id = (sp_info or {}).get("id")
        if sp_id:
            removal = _parse_sp_removal_from_plays(sp_id, side, all_plays)
            if removal:
                state.removal_info[side] = removal

    return state


def _is_sp_line_final(side: str, state: GamePitcherState) -> bool:
    """
    Return True if the starting pitcher's line is considered final.
    """
    sp_id = state.starting_pitcher.get(side, {}).get("id")
    if not sp_id:
        return False

    game_over = state.game_status in ("Final", "Game Over", "Completed Early")
    sp_still_pitching = (state.current_pitcher.get(side) == sp_id)

    if sp_still_pitching and game_over:
        return True   # complete game
    if sp_still_pitching:
        return False  # still in, game live

    removal = state.removal_info.get(side)
    if not removal:
        return False  # removal not yet detected in play-by-play

    # Any inherited runners still on base means inning is ongoing
    if removal.get("runners_outstanding"):
        return False

    return True  # removed, and no outstanding inherited runners


def _count_responsible_runner_fates(side: str, state: GamePitcherState) -> tuple[int, int, int]:
    """Returns (scored, lob, outstanding) for SP-responsible runners, from play-by-play data."""
    removal = state.removal_info.get(side, {})
    scored = removal.get("runners_scored", 0)
    lob = removal.get("runners_lob", 0)
    outstanding = len(removal.get("runners_outstanding", set()))
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

    team_abbrev = state.home_team if side == "home" else state.away_team
    team_name = state.home_team_name if side == "home" else state.away_team_name
    opp_abbrev = state.away_team if side == "home" else state.home_team

    current_pitcher_id = state.current_pitcher.get(side)
    is_cg = (current_pitcher_id == sp_id) and state.game_status in ("Final", "Game Over", "Completed Early")

    removal = state.removal_info.get(side, {})
    removal_inning = removal.get("inning", 0)
    removal_half = removal.get("half", "top")

    # For CGs use final score from linescore; otherwise use score captured at removal
    live_ls = live.get("linescore", {})
    if is_cg:
        sp_score = _linescore_runs(side, live_ls)
        opp_score = _linescore_runs("away" if side == "home" else "home", live_ls)
    else:
        sp_score = removal.get("sp_score", 0)
        opp_score = removal.get("opp_score", 0)

    scored, lob, outstanding = _count_responsible_runner_fates(side, state)

    return PitcherLine(
        pitcher_id=sp_id,
        name=sp_info["name"],
        team_abbrev=team_abbrev,
        team_name=team_name,
        opponent_abbrev=opp_abbrev,
        is_home=(side == "home"),
        ip=ip_float,
        ip_str=ip_str,
        hits=stats.get("hits", 0),
        earned_runs=stats.get("earnedRuns", 0),
        walks=stats.get("baseOnBalls", 0),
        strikeouts=stats.get("strikeOuts", 0),
        pitch_count=stats.get("numberOfPitches", 0),
        sp_score=sp_score,
        opp_score=opp_score,
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

    vs = "vs" if line.is_home else "@"
    matchup = f"({line.team_abbrev}) {vs} {line.opponent_abbrev}"

    if line.is_complete_game:
        context = "Threw a complete game."
    else:
        # Score relation from SP's team perspective
        sp, opp = line.sp_score, line.opp_score
        if sp > opp:
            score_ctx = f"ahead {sp}-{opp}"
        elif opp > sp:
            score_ctx = f"behind {opp}-{sp}"
        else:
            score_ctx = f"tied {sp}-{opp}"

        total_responsible = (
            line.responsible_runners_scored
            + line.responsible_runners_lob
            + line.responsible_runners_outstanding
        )

        if total_responsible == 0:
            context = f"Left with {line.team_name} {score_ctx}."
        elif line.responsible_runners_lob > 0 and line.responsible_runners_scored == 0:
            runner_word = "runner" if total_responsible == 1 else "runners"
            context = f"Left with {line.team_name} {score_ctx} and {total_responsible} {runner_word} on (left on base)."
        else:
            runner_word = "runner" if total_responsible == 1 else "runners"
            s = line.responsible_runners_scored
            lob = line.responsible_runners_lob
            if s == total_responsible == 1:
                fate = "scored"
            elif s == total_responsible:
                fate = "both scored" if total_responsible == 2 else "all scored"
            elif lob > 0:
                fate = f"{s} scored, {lob} left on base"
            else:
                fate = f"{s} scored"
            context = f"Left with {line.team_name} {score_ctx} and {total_responsible} {runner_word} on ({fate})."

    return f"{line.name} {matchup}: {stat_line} - {context}"


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
