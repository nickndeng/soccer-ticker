"""Linux front-end: a GTK3 AppIndicator in the GNOME/KDE top bar."""
import signal
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GLib, GdkPixbuf  # noqa: E402

from . import core  # noqa: E402

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

APP_ID = "soccer-ticker"
ICON_NAME = "applications-games-symbolic"


class SoccerTicker:
    def __init__(self):
        self.leagues = core.load_leagues()
        self.matches = []           # list of normalized live-match dicts
        self.upcoming = []          # scheduled matches (for when nothing is live)
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
        GLib.timeout_add_seconds(core.FETCH_INTERVAL_S, self._on_fetch_timer)
        GLib.timeout_add_seconds(core.ROTATE_INTERVAL_S, self._on_rotate_timer)

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

    def _soonest_upcoming(self):
        """The next match within 24h to show when nothing is live, else None."""
        within = core.upcoming_within(self.upcoming, 24)
        return within[0] if within else None

    def _refresh_display(self):
        """Update both the label and the icon for whatever's currently shown."""
        self._set_label(self._current_label_text())
        logo = None
        if self.matches:
            logo = self.matches[self.rotate_index % len(self.matches)].get("logo")
        else:
            up = self._soonest_upcoming()
            if up:
                logo = up.get("logo")
        self._set_icon(logo)  # None -> default soccer icon

    def _current_label_text(self):
        # No emoji here — the tray icon (competition logo or soccer fallback)
        # is the only graphic; a "⚽" prefix would look like a second icon.
        if self.matches:
            return core.score_label(self.matches[self.rotate_index % len(self.matches)])
        up = self._soonest_upcoming()
        if up:
            return core.upcoming_label(up)
        if self.last_error:
            return "offline"
        return ""   # no live or upcoming match within 24h: show the icon only

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
        live, upcoming, error = core.fetch_matches(self.leagues)
        GLib.idle_add(self._apply_results, live, upcoming, error)

    def _apply_results(self, live, upcoming, error):
        if live or upcoming or error is None:
            self.matches = live
            self.upcoming = upcoming
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

        within = core.upcoming_within(self.upcoming, 24)
        if self.matches:
            for i, m in enumerate(self.matches):
                if i:
                    self._add_separator()
                self._add_match(m)
        elif within:
            self._add_info("Upcoming (next 24h)")
            for u in within:
                self._add_separator()
                self._add_upcoming(u)
        else:
            self._add_info("No live or upcoming matches")
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

    def _add_match(self, m):
        """Render one live match as a block of (disabled) detail rows."""
        # Each team: crest + full name + score.
        self._add_info_icon(m.get("home_logo"), f"{m['home_full']}   {m['hg']}")
        self._add_info_icon(m.get("away_logo"), f"{m['away_full']}   {m['ag']}")
        # Competition logo + name, then the live status (verbose).
        self._add_info_icon(m.get("logo_menu"), f"{m['competition']}   ·   {m['status_detail']}")

        for g in m.get("scorers", []):
            side = m["home"] if g["side"] == "home" else m["away"]
            mark = "⚽(OG)" if g["own"] else "⚽"
            minute = f"{g['minute']} " if g["minute"] else ""
            self._add_info(f"         {mark} {minute}{g['name']} ({side})")

        for c in m.get("cards", []):
            side = m["home"] if c["side"] == "home" else m["away"]
            mark = "🟥" if c["red"] else "🟨"
            minute = f"{c['minute']} " if c["minute"] else ""
            self._add_info(f"         {mark} {minute}{c['name']} ({side})")

        ph, pa = m.get("possession", (None, None))
        if ph and pa:
            self._add_info(f"      Possession  {round(float(ph))}% – {round(float(pa))}%")
        sh, sa = m.get("shots", (None, None))
        th, ta = m.get("shots_on_target", (None, None))
        if sh and sa:
            extra = f"  ({th}/{ta} on target)" if th and ta else ""
            self._add_info(f"      Shots  {sh} – {sa}{extra}")

        fh, fa = m.get("form", (None, None))
        if fh or fa:
            self._add_info(f"      Form  {fh or '–'} – {fa or '–'}  (home–away, recent)")

        odds = m.get("odds")
        if odds:
            bits = []
            if odds.get("summary"):
                bits.append(odds["summary"])
            if odds.get("over_under") is not None:
                bits.append(f"O/U {odds['over_under']}")
            line = "  ·  ".join(bits)
            if odds.get("provider"):
                line += f"   ({odds['provider']})"
            if line:
                self._add_info(f"      💰 {line}")

        if m.get("venue"):
            self._add_info(f"      📍 {m['venue']}")
        if m.get("tv"):
            self._add_info(f"      📺 {m['tv']}")

    def _add_upcoming(self, u):
        """Render one scheduled match as a compact (disabled) block."""
        self._add_info_icon(u.get("home_logo"), u["home_full"])
        self._add_info_icon(u.get("away_logo"), u["away_full"])
        self._add_info_icon(u.get("logo_menu"),
                            f"{u['competition']}   ·   Kicks off {core.kickoff_when(u)}")
        if u.get("venue"):
            self._add_info(f"      📍 {u['venue']}")
        if u.get("tv"):
            self._add_info(f"      📺 {u['tv']}")

    def _add_info(self, text):
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _add_info_icon(self, icon_path, text):
        """A disabled menu row with a small icon (e.g. competition logo) + text."""
        item = Gtk.MenuItem()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if icon_path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_path, 16, 16)
                box.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            except Exception:
                pass
        box.pack_start(Gtk.Label(label=text), False, False, 0)
        item.add(box)
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
