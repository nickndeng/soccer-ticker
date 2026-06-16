"""Platform-agnostic core: config, ESPN client, match normalization, logos.

This module has NO GUI/toolkit dependencies (no GTK, no rumps) so it can be
imported on Linux and macOS alike. The per-platform front-ends consume it.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ESPN's undocumented-but-public scoreboard API. state is one of pre/in/post;
# "in" means the match is being played right now (includes half-time).
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
_UA = {"User-Agent": "soccer-ticker"}

# (ESPN league slug, friendly name) — which competitions to watch. Override via
# the "leagues" list in the config file.
DEFAULT_LEAGUES = [
    ("fifa.world", "World Cup"),
    ("uefa.champions", "Champions League"),
    ("eng.1", "Premier League"),
    ("esp.1", "La Liga"),
    ("ita.1", "Serie A"),
    ("ger.1", "Bundesliga"),
    ("fra.1", "Ligue 1"),
    ("usa.1", "MLS"),
]

FETCH_INTERVAL_S = 30      # ESPN updates frequently; poll every 30s
ROTATE_INTERVAL_S = 6      # how often the bar label cycles through matches

APP = "soccer-ticker"


def _base_dirs():
    """Return (config_dir, cache_dir) following each platform's conventions."""
    home = Path.home()
    if sys.platform == "darwin":
        return (home / "Library" / "Application Support" / APP,
                home / "Library" / "Caches" / APP)
    # Linux / other XDG systems
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / APP
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / APP
    return config, cache


CONFIG_DIR, CACHE_DIR = _base_dirs()
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_leagues():
    """Read the league watch-list from config, falling back to defaults."""
    try:
        data = json.loads(CONFIG_PATH.read_text())
        leagues = data.get("leagues")
        if isinstance(leagues, list) and leagues:
            # Accept either ["eng.1", ...] or [["eng.1","Premier League"], ...].
            out = []
            for item in leagues:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    out.append((str(item[0]), str(item[1])))
                elif isinstance(item, str):
                    out.append((item, item))
            if out:
                return out
    except (OSError, ValueError):
        pass
    return DEFAULT_LEAGUES


def _to_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def pick_logo(league, prefer):
    """Return the logo URL matching the preferred rel ('dark'/'default')."""
    logos = league.get("logos") or []
    for L in logos:
        if prefer in (L.get("rel") or []):
            return L.get("href")
    return logos[0].get("href") if logos else None


def ensure_logo(slug, url, variant):
    """Download a league logo variant to the cache once; return its file path."""
    if not url:
        return None
    path = CACHE_DIR / f"{slug.replace('.', '_')}_{variant}.png"
    if path.exists():
        return str(path)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=8, headers=_UA)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return str(path)
    except Exception:
        return None


def _tag(team):
    t = team.get("team", {})
    return t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "?"


def _full(team):
    return team.get("team", {}).get("displayName", "?")


def _broadcasts(comp):
    names = []
    for b in comp.get("broadcasts") or []:
        names.extend(b.get("names") or [])
    return ", ".join(dict.fromkeys(names))  # de-duped, order-preserving


def _venue(comp, ev):
    return (comp.get("venue") or ev.get("venue") or {}).get("fullName")


def _sides(comp):
    """Return (home, away) competitor dicts, raising if either is missing."""
    sides = comp["competitors"]
    home = next(t for t in sides if t.get("homeAway") == "home")
    away = next(t for t in sides if t.get("homeAway") == "away")
    return home, away


def ensure_team_logo(team):
    """Cache a team's crest (by team id) and return its file path, or None."""
    tid, url = team.get("id"), team.get("logo")
    if not tid or not url:
        return None
    return ensure_logo(f"team_{tid}", url, "crest")


def _attach_crests(m, comp):
    """Add home/away team crest paths to a normalized match dict."""
    try:
        home, away = _sides(comp)
    except (KeyError, StopIteration):
        m["home_logo"] = m["away_logo"] = None
        return
    m["home_logo"] = ensure_team_logo(home.get("team", {}))
    m["away_logo"] = ensure_team_logo(away.get("team", {}))


def normalize(ev, league_name, logo=None, logo_menu=None):
    """Turn one ESPN event into a flat dict, or None if it isn't in progress."""
    try:
        comp = ev["competitions"][0]
        status_block = ev.get("status", {})
        status = status_block.get("type", {})
        if status.get("state") != "in":   # only matches in progress
            return None
        home, away = _sides(comp)
    except (KeyError, IndexError, StopIteration):
        return None

    tag, full = _tag, _full

    # Concise clock for the bar, verbose status for the dropdown. At half-time
    # the bar stays compact ("HT") while the dropdown spells out "Half Time".
    is_ht = "HALFTIME" in (status.get("name") or "")
    if is_ht:
        clock = "HT"
        status_detail = "Half Time"
    else:
        clock = status_block.get("displayClock") or status.get("shortDetail") or "LIVE"
        desc = status.get("description") or "Live"
        status_detail = f"{desc} · {clock}" if clock and clock not in desc else desc

    # Per-team statistics (possession, shots, ...), keyed by ESPN stat name.
    def stats(team):
        return {s.get("name"): s.get("displayValue") for s in team.get("statistics") or []}

    hs, as_ = stats(home), stats(away)

    # Goals & cards from the play-by-play "details". Goals carry a
    # scoreValue >= 1 (or "goal" in the type text); cards say "Card".
    home_id = str(home.get("team", {}).get("id"))

    def side_of(play):
        return "home" if str((play.get("team") or {}).get("id")) == home_id else "away"

    def player(play, fallback):
        names = [a.get("displayName", "?") for a in play.get("athletesInvolved") or []]
        return names[0] if names else fallback

    scorers, cards = [], []
    for p in comp.get("details") or []:
        text = (p.get("type") or {}).get("text", "") or ""
        minute = (p.get("clock") or {}).get("displayValue", "")
        low = text.lower()
        if (p.get("scoreValue") or 0) >= 1 or "goal" in low:
            scorers.append({
                "name": player(p, text), "minute": minute,
                "side": side_of(p), "own": "own" in low,
            })
        elif "card" in low:
            cards.append({
                "name": player(p, text), "minute": minute,
                "side": side_of(p), "red": "red" in low,
            })

    odds_raw = (comp.get("odds") or [{}])[0]
    odds = None
    if odds_raw.get("details") or odds_raw.get("overUnder") is not None:
        odds = {
            "summary": odds_raw.get("details"),
            "over_under": odds_raw.get("overUnder"),
            "provider": (odds_raw.get("provider") or {}).get("displayName"),
        }

    return {
        "home": tag(home),
        "away": tag(away),
        "home_full": full(home),
        "away_full": full(away),
        "hg": _to_int(home.get("score")),
        "ag": _to_int(away.get("score")),
        "clock": clock,                 # compact, for the bar label
        "status_detail": status_detail,  # verbose, for the dropdown
        "competition": league_name,
        "logo": logo,            # bar icon (light-on-dark)
        "logo_menu": logo_menu,  # dropdown icon (full colour)
        "scorers": scorers,
        "cards": cards,
        "form": (home.get("form"), away.get("form")),
        "odds": odds,
        "possession": (hs.get("possessionPct"), as_.get("possessionPct")),
        "shots": (hs.get("totalShots"), as_.get("totalShots")),
        "shots_on_target": (hs.get("shotsOnTarget"), as_.get("shotsOnTarget")),
        "venue": _venue(comp, ev),
        "tv": _broadcasts(comp),
    }


def normalize_upcoming(ev, league_name, logo=None, logo_menu=None):
    """Turn one ESPN event into an 'upcoming match' dict, or None if not scheduled."""
    try:
        comp = ev["competitions"][0]
        status = ev.get("status", {}).get("type", {})
        if status.get("state") != "pre":   # only not-yet-started matches
            return None
        home, away = _sides(comp)
        kickoff = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
    except (KeyError, IndexError, StopIteration, ValueError):
        return None

    return {
        "home": _tag(home),
        "away": _tag(away),
        "home_full": _full(home),
        "away_full": _full(away),
        "competition": league_name,
        "logo": logo,
        "logo_menu": logo_menu,
        "kickoff": kickoff,          # tz-aware UTC datetime
        "venue": _venue(comp, ev),
        "tv": _broadcasts(comp),
    }


def kickoff_when(m):
    """Human kickoff time in local tz: 'HH:MM' today, else 'Wed HH:MM'."""
    ko = m["kickoff"].astimezone()
    if ko.date() == datetime.now().astimezone().date():
        return ko.strftime("%H:%M")
    return ko.strftime("%a %H:%M")


def upcoming_label(m):
    """Compact one-line label for the menu-bar/top-bar: 'ARS v CHE 19:00'."""
    return f"{m['home']} v {m['away']} {kickoff_when(m)}"


def upcoming_within(upcoming, within_hours=24):
    """Upcoming matches kicking off within the window, soonest first."""
    cutoff = datetime.now(timezone.utc) + timedelta(hours=within_hours)
    return sorted((m for m in upcoming if m["kickoff"] <= cutoff),
                  key=lambda m: m["kickoff"])


def fetch_matches(leagues):
    """Poll every watched league and return (live, upcoming, error).

    Queries a today→tomorrow window so upcoming games in the next 24h (which may
    fall on tomorrow's date) are included. `error` is a string only when *every*
    league request failed (so a single flaky league doesn't blank the display).
    """
    now = datetime.now(timezone.utc)
    date_range = f"{now:%Y%m%d}-{now + timedelta(days=1):%Y%m%d}"

    live, upcoming, errors = [], [], []
    with requests.Session() as session:
        for slug, name in leagues:
            try:
                resp = session.get(ESPN_URL.format(slug=slug),
                                   params={"dates": date_range}, timeout=8, headers=_UA)
                resp.raise_for_status()
                data = resp.json()
                league = (data.get("leagues") or [{}])[0]
                # Two variants: light-on-dark for the bar, full-colour for the
                # dropdown menu (which sits on a light background).
                logo = ensure_logo(slug, pick_logo(league, "dark"), "dark")
                logo_menu = ensure_logo(slug, pick_logo(league, "default"), "color")
                for ev in data.get("events", []):
                    comp = (ev.get("competitions") or [{}])[0]
                    m = normalize(ev, name, logo, logo_menu)
                    if m is not None:
                        _attach_crests(m, comp)
                        live.append(m)
                        continue
                    u = normalize_upcoming(ev, name, logo, logo_menu)
                    if u is not None:
                        _attach_crests(u, comp)
                        upcoming.append(u)
            except Exception as exc:  # network / parse / HTTP errors
                errors.append(f"{slug}: {exc}")
    error = "; ".join(errors) if len(errors) == len(leagues) else None
    return live, upcoming, error


def score_label(m):
    """Compact one-line score for the menu-bar/top-bar label."""
    return f"{m['home']} {m['hg']}-{m['ag']} {m['away']} {m['clock']}"
