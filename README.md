# ⚽ Soccer Ticker

Live soccer scores in your Linux system tray (top bar). A small AppIndicator
shows the score of an in-progress match and rotates through all of them; the
dropdown lists every live game with its competition and clock.

Data comes from **ESPN's free public scoreboard API** — real-time, **no API key
or signup required**.

```
┌─ Top bar ───────────────────────────┐
│ Activities   ⚽ CIV 0-0 ECU 40'  🔋 🔊 │
└─────────────────────────────────────┘
        click ▾
        ⚽ CIV 0 - 0 ECU   40'
              World Cup
        ─────────────────
        Updated 23:42:07
        Refresh now
        ─────────────────
        Quit
```

## 1. Install dependencies

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
pip3 install --user requests        # usually already present
```

On Ubuntu GNOME, tray icons are shown by the **Ubuntu AppIndicators**
extension, which ships and is enabled by default. On vanilla GNOME, install the
[AppIndicator Support](https://extensions.gnome.org/extension/615/appindicator-support/)
extension.

## 2. Run

```bash
python3 soccer_ticker.py
```

The indicator appears in the top bar. With no live matches it shows
`⚽ no live games`; if the network is down it shows `⚽ offline`.

## 3. Start automatically on login (optional)

```bash
cp soccer-ticker.desktop ~/.config/autostart/
```

(The bundled `.desktop` assumes the project lives in `~/git/soccer-ticker`.
Adjust the `Exec=` line if you cloned it elsewhere.)

## Configuration (optional)

By default it watches the World Cup, Champions League, and the top five European
leagues + MLS. To change that, drop a config file at
`~/.config/soccer-ticker/config.json` (see `config.example.json`) with a
`leagues` list of ESPN league slugs, e.g.:

```json
{ "leagues": [["fifa.world", "World Cup"], ["eng.1", "Premier League"]] }
```

Other ESPN slugs include `eng.2` (Championship), `ned.1` (Eredivisie),
`por.1` (Primeira Liga), `bra.1` (Brasileirão), `uefa.europa`, `mex.1`.

## Notes

- Polls every 30s across the watched leagues. ESPN's API is undocumented but
  public and free; there's no published rate limit, but keep the league list
  reasonable.
- Only matches with state `in` (being played, including half-time) appear.
- A failure on one league is ignored; the label only shows `⚽ offline` if
  every league request fails.
- Why not football-data.org? Its free tier does **not** provide real-time
  in-play status — a World Cup match 40 minutes in still reported as
  "scheduled". ESPN's feed updates live.
