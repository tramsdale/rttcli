# rtt

A command-line interface for [Real Time Trains](https://www.realtimetrains.co.uk/), showing live departure boards and full service details from the terminal.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- A Real Time Trains API token from [api-portal.rtt.io](https://api-portal.rtt.io)

## Setup

Clone the repo and symlink the script somewhere on your `$PATH`:

```bash
git clone git@github.com:tramsdale/rttcli.git
cd rttcli
ln -s "$PWD/rtt.py" ~/.local/bin/rtt
```

Save your API token:

```bash
rtt config --token YOUR_TOKEN
```

The token and a cached access token are stored in `~/.config/rtt/config.json`. The CLI handles token refresh automatically.

## Usage

```
rtt FROM TO [options]
```

### Examples

```bash
rtt PAD BRI                        # next trains from Paddington to Bristol
rtt PAD BRI --after 2100           # trains after 21:00 today
rtt PAD BRI --tomorrow             # tomorrow's trains from 06:00
rtt PAD BRI --friday               # next Friday's trains
rtt PAD BRI --date 9/6/26          # trains on a specific date (DD/MM/YY)
rtt PAD BRI --detail 1             # full calling points for the first train
rtt PAD BRI --after 1800 --detail 2
```

### Options

| Option | Description |
|---|---|
| `--after HHMM` | Show trains departing after this time (e.g. `2100`) |
| `--tomorrow` | Show trains for tomorrow |
| `--monday` … `--sunday` | Show trains for the next occurrence of that weekday |
| `--date DD/MM/YY` | Show trains for a specific date |
| `--detail N` | Show full calling points for train #N in the list |

Station codes are standard CRS codes (e.g. `PAD`, `BRI`, `WAT`, `EUS`).

## Output

**Departure board** — shows scheduled/actual departure time, headcode, destination, operator, platform, and live status (on time, delayed with reason, cancelled).

**Detail view** (`--detail N`) — shows all calling points with arrival and departure times, platforms, delay reasons, and highlights your destination station.
