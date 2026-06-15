"""macOS front-end: a native menu-bar app built on rumps (NSStatusItem).

Mirrors the Linux front-end — the score shows as the menu-bar title with the
competition logo as the icon, and the dropdown lists every live match with the
same detail (scorers, cards, stats, form, odds, venue, TV).

Note: rumps' run loop is single-threaded (AppKit must be touched only on the
main thread), so network fetches run on a background thread and hand results
back to a 1-second main-thread timer that applies them and drives rotation.
"""
import threading
from datetime import datetime

try:
    import rumps
except ImportError:  # surfaced with a friendly message in main()
    rumps = None

from . import core

ZWSP = "​"  # zero-width space: makes disabled rows' titles unique to rumps


class SoccerTickerMac(rumps.App):
    def __init__(self):
        # quit_button=None: we add our own Quit so menu.clear() can't drop it.
        super().__init__("Soccer Ticker", title="…", quit_button=None)
        self.leagues = core.load_leagues()
        self.matches = []
        self.rotate_index = 0
        self.last_updated = None
        self.last_error = None

        self._pending = None          # (matches, error) handed over by the fetch thread
        self._lock = threading.Lock()
        self._tick = 0
        self._uid = 0

        self._fetch_async()
        self._rebuild_menu()
        # 1s timer applies fetched results + rotates; slow timer triggers fetches.
        rumps.Timer(self._on_tick, 1).start()
        rumps.Timer(self._fetch_async, core.FETCH_INTERVAL_S).start()

    # ---- networking (background thread) --------------------------------
    def _fetch_async(self, *_):
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        matches, error = core.fetch_live_matches(self.leagues)
        with self._lock:
            self._pending = (matches, error)

    # ---- main-thread tick: apply results + rotate ----------------------
    def _on_tick(self, _):
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            matches, error = pending
            if matches or error is None:
                self.matches = matches
                self.last_updated = datetime.now()
                self.rotate_index = 0
            self.last_error = error
            self._refresh_display()
            self._rebuild_menu()

        self._tick += 1
        if self.matches and self._tick % core.ROTATE_INTERVAL_S == 0:
            self.rotate_index = (self.rotate_index + 1) % len(self.matches)
            self._refresh_display()

    # ---- menu-bar title + icon -----------------------------------------
    def _refresh_display(self):
        self.title = self._title_text()
        logo = None
        if self.matches:
            logo = self.matches[self.rotate_index % len(self.matches)].get("logo_menu")
        try:
            self.icon = logo            # path or None; rumps scales to the bar
            self.template = False       # keep the logo in colour, not monochrome
        except Exception:
            pass

    def _title_text(self):
        if self.last_error and not self.matches:
            return "offline"
        if not self.matches:
            return "no live games"
        return core.score_label(self.matches[self.rotate_index % len(self.matches)])

    # ---- dropdown menu -------------------------------------------------
    def _uniq(self, text):
        """rumps keys items by title, so make disabled rows unique invisibly."""
        self._uid += 1
        return text + ZWSP * self._uid

    def _info(self, text):
        # A MenuItem with no callback is rendered disabled by AppKit.
        return rumps.MenuItem(self._uniq(text))

    def _rebuild_menu(self):
        self.menu.clear()
        self._uid = 0
        items = []

        if self.matches:
            for i, m in enumerate(self.matches):
                if i:
                    items.append(rumps.separator)
                items.extend(self._match_items(m))
        else:
            items.append(self._info("No live matches right now"))
            if self.last_error:
                items.append(self._info(f"   ({self.last_error[:48]})"))

        items.append(rumps.separator)
        stamp = self.last_updated.strftime("%H:%M:%S") if self.last_updated else "—"
        items.append(self._info(f"Updated {stamp}"))
        items.append(rumps.MenuItem("Refresh now", callback=self._fetch_async))
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))

        for it in items:
            self.menu.add(it)

    def _match_items(self, m):
        out = [self._info(f"{m['home_full']}  {m['hg']} - {m['ag']}  {m['away_full']}")]
        # Competition logo (native icon) + name + status.
        out.append(rumps.MenuItem(
            self._uniq(f"{m['competition']}   ·   {m['status_detail']}"),
            icon=m.get("logo_menu"), dimensions=[16, 16],
        ))

        for g in m.get("scorers", []):
            side = m["home"] if g["side"] == "home" else m["away"]
            mark = "⚽(OG)" if g["own"] else "⚽"
            minute = f"{g['minute']} " if g["minute"] else ""
            out.append(self._info(f"    {mark} {minute}{g['name']} ({side})"))

        for c in m.get("cards", []):
            side = m["home"] if c["side"] == "home" else m["away"]
            mark = "🟥" if c["red"] else "🟨"
            minute = f"{c['minute']} " if c["minute"] else ""
            out.append(self._info(f"    {mark} {minute}{c['name']} ({side})"))

        ph, pa = m.get("possession", (None, None))
        if ph and pa:
            out.append(self._info(f"    Possession  {round(float(ph))}% – {round(float(pa))}%"))
        sh, sa = m.get("shots", (None, None))
        th, ta = m.get("shots_on_target", (None, None))
        if sh and sa:
            extra = f"  ({th}/{ta} on target)" if th and ta else ""
            out.append(self._info(f"    Shots  {sh} – {sa}{extra}"))

        fh, fa = m.get("form", (None, None))
        if fh or fa:
            out.append(self._info(f"    Form  {fh or '–'} – {fa or '–'}  (home–away, recent)"))

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
                out.append(self._info(f"    💰 {line}"))

        if m.get("venue"):
            out.append(self._info(f"    📍 {m['venue']}"))
        if m.get("tv"):
            out.append(self._info(f"    📺 {m['tv']}"))
        return out


def main():
    if rumps is None:
        raise SystemExit("rumps is required on macOS. Install with:\n  pip3 install rumps")
    SoccerTickerMac().run()
