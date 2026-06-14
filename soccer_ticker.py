#!/usr/bin/env python3
"""Soccer Ticker — live soccer scores in the Linux system tray.

Shows live match scores in the GNOME/KDE top bar via an AppIndicator,
with a dropdown listing every in-progress match. Data: ESPN's free public
scoreboard API (no key required, real-time).
"""
import json
import os
import signal
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

# AppIndicator bindings: prefer the maintained Ayatana fork, fall back to the
# legacy Canonical one. One of these must be installed (see README).
AppIndicator = None
for _ns, _ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(_ns, _ver)
        AppIndicator = getattr(__import__("gi.repository", fromlist=[_ns]), _ns)
        break
    except (ValueError, ImportError, AttributeError):
        continue

import requests  # noqa: E402

# ESPN's undocumented-but-public scoreboard API. state is one of pre/in/post;
# "in" means the match is being played right now (includes half-time).
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"

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

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "soccer-ticker"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(GLib.get_user_cache_dir()) / "soccer-ticker"  # cached league logos

FETCH_INTERVAL_S = 30      # ESPN updates frequently; poll every 30s
ROTATE_INTERVAL_S = 6      # how often the top-bar label cycles matches
APP_ID = "soccer-ticker"
ICON_NAME = "applications-games-symbolic"


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


class SoccerTicker:
    def __init__(self):
        self.leagues = load_leagues()
        self.matches = []           # list of normalized live-match dicts
        self.rotate_index = 0
        self.last_updated = None
        self.last_error = None
        self.current_icon = ICON_NAME   # what the tray icon currently shows

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, ICON_NAME, AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._set_label("…")
        self._rebuild_menu()

        # Kick off an immediate fetch, then poll + rotate on timers.
        GLib.idle_add(self.refresh_async)
        GLib.timeout_add_seconds(FETCH_INTERVAL_S, self._on_fetch_timer)
        GLib.timeout_add_seconds(ROTATE_INTERVAL_S, self._on_rotate_timer)

    # ---- top-bar label -------------------------------------------------
    def _set_label(self, text):
        # guide string reserves layout width so the bar doesn't jump around.
        self.indicator.set_label(text, "WWW 0-0 WWW 90'")

    def _set_icon(self, path):
        """Switch the tray icon to a logo file path, or the default icon name."""
        target = path or ICON_NAME
        if target != self.current_icon:
            self.indicator.set_icon_full(target, "competition")
            self.current_icon = target

    def _refresh_display(self):
        """Update both the label and the icon for the currently shown match."""
        self._set_label(self._current_label_text())
        logo = None
        if self.matches:
            logo = self.matches[self.rotate_index % len(self.matches)].get("logo")
        self._set_icon(logo)

    def _current_label_text(self):
        # No emoji here — the tray icon (competition logo or soccer fallback)
        # is the only graphic; a "⚽" prefix would look like a second icon.
        if self.last_error and not self.matches:
            return "offline"
        if not self.matches:
            return "no live games"
        m = self.matches[self.rotate_index % len(self.matches)]
        return f"{m['home']} {m['hg']}-{m['ag']} {m['away']} {m['clock']}"

    # ---- timers --------------------------------------------------------
    def _on_fetch_timer(self):
        self.refresh_async()
        return True  # keep the timer alive

    def _on_rotate_timer(self):
        if self.matches:
            self.rotate_index = (self.rotate_index + 1) % len(self.matches)
            self._refresh_display()
        return True

    # ---- networking (off the GTK main thread) --------------------------
    def refresh_async(self, *_):
        threading.Thread(target=self._fetch_worker, daemon=True).start()
        return False  # so this can be used as a one-shot idle callback

    def _fetch_worker(self):
        matches = []
        errors = []
        with requests.Session() as session:
            for slug, name in self.leagues:
                try:
                    resp = session.get(
                        ESPN_URL.format(slug=slug),
                        timeout=8,
                        headers={"User-Agent": "soccer-ticker"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    league = (data.get("leagues") or [{}])[0]
                    logo = self._ensure_logo(slug, self._pick_logo(league))
                    for ev in data.get("events", []):
                        m = self._normalize(ev, name, logo)
                        if m is not None:
                            matches.append(m)
                except Exception as exc:  # network / parse / HTTP errors
                    errors.append(f"{slug}: {exc}")
        # Only treat it as a hard error if every league failed.
        error = "; ".join(errors) if len(errors) == len(self.leagues) else None
        GLib.idle_add(self._apply_results, matches, error)

    @staticmethod
    def _pick_logo(league):
        """Prefer the dark-background (light-colored) logo for the top bar."""
        logos = league.get("logos") or []
        for L in logos:
            if "dark" in (L.get("rel") or []):
                return L.get("href")
        return logos[0].get("href") if logos else None

    @staticmethod
    def _ensure_logo(slug, url):
        """Download a league logo to the cache once; return its file path."""
        if not url:
            return None
        path = CACHE_DIR / f"{slug.replace('.', '_')}.png"
        if path.exists():
            return str(path)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            resp = requests.get(url, timeout=8, headers={"User-Agent": "soccer-ticker"})
            resp.raise_for_status()
            path.write_bytes(resp.content)
            return str(path)
        except Exception:
            return None

    @staticmethod
    def _normalize(ev, league_name, logo=None):
        try:
            comp = ev["competitions"][0]
            status = ev.get("status", {}).get("type", {})
            if status.get("state") != "in":   # only matches in progress
                return None
            sides = comp["competitors"]
            home = next(t for t in sides if t.get("homeAway") == "home")
            away = next(t for t in sides if t.get("homeAway") == "away")
        except (KeyError, IndexError, StopIteration):
            return None

        def tag(team):
            t = team.get("team", {})
            return t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "?"

        def full(team):
            return team.get("team", {}).get("displayName", "?")

        return {
            "home": tag(home),
            "away": tag(away),
            "home_full": full(home),
            "away_full": full(away),
            "hg": _to_int(home.get("score")),
            "ag": _to_int(away.get("score")),
            "clock": status.get("shortDetail") or "LIVE",
            "competition": league_name,
            "logo": logo,
        }

    def _apply_results(self, matches, error):
        if matches or error is None:
            self.matches = matches
            self.last_updated = datetime.now()
            self.rotate_index = 0
        self.last_error = error
        self._refresh_display()
        self._rebuild_menu()
        return False

    # ---- dropdown menu -------------------------------------------------
    def _rebuild_menu(self):
        for child in self.menu.get_children():
            self.menu.remove(child)

        if self.matches:
            for m in self.matches:
                self._add_info(
                    f"⚽ {m['home']} {m['hg']} - {m['ag']} {m['away']}   {m['clock']}"
                )
                if m["competition"]:
                    self._add_info(f"      {m['competition']}")
        else:
            self._add_info("No live matches right now")
            if self.last_error:
                self._add_info(f"   ({self.last_error[:48]})")

        self._add_separator()
        stamp = self.last_updated.strftime("%H:%M:%S") if self.last_updated else "—"
        self._add_info(f"Updated {stamp}")

        refresh = Gtk.MenuItem(label="Refresh now")
        refresh.connect("activate", self.refresh_async)
        self.menu.append(refresh)

        self._add_separator()
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()

    def _add_info(self, text):
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _add_separator(self):
        self.menu.append(Gtk.SeparatorMenuItem())


def main():
    if AppIndicator is None:
        raise SystemExit(
            "AppIndicator bindings not found. Install with:\n"
            "  sudo apt install gir1.2-ayatanaappindicator3-0.1"
        )
    SoccerTicker()
    # Let Ctrl+C kill the GTK loop from the terminal.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()


if __name__ == "__main__":
    main()
