# rtt

A command-line interface for [Real Time Trains](https://www.realtimetrains.co.uk/), showing live departure boards and full service details from the terminal.

## Requirements

- [uv](https://docs.astral.sh/uv/) — Python package manager (handles dependencies automatically)
- A Real Time Trains API account (free) — see below

## Getting an API key

1. Go to [api-portal.rtt.io](https://api-portal.rtt.io) and create a free account
2. Once logged in, go to **My Account → Subscriptions**
3. Subscribe to the **RTT API** product (the free tier is sufficient)
4. Your API token will be shown under **My Account → Profile** — copy the **Primary key**

## Installation

Clone the repo and symlink the script somewhere on your `$PATH`:

```bash
git clone git@github.com:tramsdale/rttcli.git
cd rttcli
ln -s "$PWD/rtt.py" ~/.local/bin/rtt
```

> `~/.local/bin` must be on your `$PATH`. On most Linux/macOS systems it is by default; if not, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

Save your API token:

```bash
rtt config --token YOUR_TOKEN
```

The token is stored in `~/.config/rtt/config.json`. The CLI exchanges it for a short-lived access token automatically and caches it — you don't need to do anything else.

## Usage

```
rtt FROM TO [options]
rtt ROUTE_NAME [options]
```

Station codes are standard CRS codes (three letters, e.g. `PAD`, `BRI`, `WAT`, `EUS`, `KGX`). You can look these up at [realtimetrains.co.uk](https://www.realtimetrains.co.uk/).

### Examples

```bash
rtt PAD BRI                        # next trains from Paddington to Bristol
rtt PAD BRI --after 2100           # trains after 21:00 today
rtt PAD BRI --arriveby 1000        # trains arriving in Bristol by 10:00
rtt PAD BRI --tomorrow             # tomorrow's trains from 06:00
rtt PAD BRI --friday               # next Friday's trains
rtt PAD BRI --date 9/6/26          # trains on a specific date (DD/MM/YY)
rtt PAD BRI --detail 1             # full calling points for the first train
rtt PAD BRI --after 1800 --detail 2
rtt PAD BRI --friday --arriveby 1200
rtt CBG LON --tuesday --arriveby 0830   # LON = KGX + STP, merged into one ordered list
```

### Options

| Option | Description |
|---|---|
| `--after HHMM` | Show trains departing after this time (e.g. `2100`) |
| `--arriveby HHMM` | Show trains arriving at the destination by this time, plus the next train after |
| `--tomorrow` | Show trains for tomorrow from 06:00 |
| `--monday` … `--sunday` | Show trains for the next occurrence of that weekday (always next week if today matches) |
| `--date DD/MM/YY` | Show trains for a specific date |
| `--detail N` | Show full calling points for train #N in the list |
| `--share` | With `--detail N`, print a public link to track that train's live status (requires the [server](server/README.md) to be deployed) |

## Output

**Departure board** — scheduled and actual departure times, headcode, destination, operator, platform, and live status (on time, delayed with reason, cancelled).

**Detail view** (`--detail N`) — all calling points with arrival and departure times, platforms, and delay reasons. Your boarding station is marked `↑` in green and your alighting station `↓` in cyan.

## Station groups

Some station codes are aliases for a set of nearby stations, searched together and merged into one time-ordered list. Currently:

| Alias | Expands to |
|---|---|
| `LON` | King's Cross (`KGX`) + St Pancras International (`STP`) |

Use the alias as `FROM` or `TO` in place of a CRS code, e.g. `rtt CBG LON --arriveby 0830`. Each row is annotated with the real station the train actually uses, e.g. `08:03 (KGX)`. `--detail`/`--share` use that real station for highlighting and links, not the alias.

## Sharing a live tracking link

`rtt PAD BRI --detail 1 --share` prints a public, no-login URL that renders a live-updating page for that train — handy for sharing with someone who wants to follow the journey without the CLI:

```bash
rtt PAD BRI --detail 1 --share
# Share link: https://rtt.tcla.me/t/C00166/2026-08-09?from=PAD&to=BRI
```

The page highlights the next stop and shows live status. Auto-refresh is off by default (reload manually, or tap "resume auto-refresh" on the page to poll every 30s) — this keeps an open tab from repeatedly hitting the RTT API on its own. It points at the [deployed server](server/README.md); by default this is `https://rtt.tcla.me`, but you can point it elsewhere with:

```bash
rtt config --share-url https://your-deployment.example.com
```

## Route aliases

For a journey with an interchange (e.g. train + underground), you can save a named two-leg route:

```bash
rtt config --add-route NAME FROM1 TO1 FROM2 TO2 [--transfer MINS]
```

The `--transfer` flag sets the minimum interchange time in minutes (default: 25). Once saved, run the route by name:

```bash
rtt NAME
rtt NAME --friday --after 0800
rtt NAME --detail 2
```

### Example

```bash
# Save a commute route: Bristol → Paddington, then King's Cross → Cambridge
rtt config --add-route commute BRI PAD KGX CBG --transfer 30

# Run it
rtt commute
rtt commute --tomorrow --detail 1
```

### Route output

The route table shows each leg 1 train alongside the leg 2 connection it makes, with a **Margin** column showing spare time beyond the required transfer (e.g. `+8m (38m)` means 8 minutes to spare, 38 minutes total between trains). Rows where the transfer time is too tight are shown greyed out above the row for the connection you would actually catch.

List your saved routes:

```bash
rtt config --list-routes
```
