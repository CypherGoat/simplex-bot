# CypherGoat SimpleX Bot

A [SimpleX Chat](https://simplex.chat) bot that lets users swap cryptocurrency via [CypherGoat](https://cyphergoat.com) — privately, without accounts

Users add the bot as a SimpleX contact and can compare rates across 23+ exchanges, execute swaps, and track transactions — all from within their SimpleX chat.

## Features

- Compare live swap rates across 23+ exchanges
- Interactive exchange selection — see all rates then pick
- Direct swap with a specific exchange
- Transaction tracking by CGID
- All cyphergoat supported coins with multi-network support (ETH, Tron, BSC, Lightning, etc.)
- Auto-accepts contact requests and sends a welcome message
- Rate limiting, swap timeout, and persistent state across restarts

## Requirements

- Python 3.11+
- [`simplex-chat`](https://github.com/simplex-chat/simplex-chat/releases) binary
- CypherGoat API key — request one at support@cyphergoat.com

## Setup

### 1. Install simplex-chat

Download the latest binary from the [SimpleX releases page](https://github.com/simplex-chat/simplex-chat/releases) and put it on your `$PATH`.

First-run setup (creates a profile):
```bash
simplex-chat
```

Follow the prompts to create your bot's profile, then exit.

### 2. Clone the repo

```bash
git clone https://github.com/cyphergoat/simplex-bot
cd simplex-bot
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```env
CYPHERGOAT_API_KEY=your_api_key_here
SIMPLEX_WS_URL=ws://localhost:5225
```
You can request an api key sending us an email to support@cyphergoat.com


### 5. Run

Start simplex-chat in WebSocket mode in one terminal:
```bash
simplex-chat -p 5225
```

Start the bot in another:
```bash
python3 bot.py
```

## Usage

Add the bot as a contact in SimpleX. It will auto-accept and send a welcome message.

### Commands

| Command | Description |
|---------|-------------|
| `estimate <amount> <from> <to>` | Compare rates across all exchanges |
| `swap <amount> <from> <to> <address>` | See rates and pick an exchange |
| `swap <amount> <from> <to> <exchange> <address>` | Swap directly with a specific exchange |
| `track <cgid>` | Check swap status by CypherGoat ID |
| `coins [search]` | List supported coins and networks |
| `help` | Show the help message |
| `cancel` | Cancel a pending exchange selection |

### Examples

```
estimate 0.1 btc xmr
swap 0.1 btc xmr <your_xmr_address>
swap 100 usdt:tron xmr QuickEx <your_xmr_address>
track 443eeccf-e620-403d-a2d3-e25141152f28
coins usdt
```

### Multi-network coins

Some coins exist on multiple blockchains. Use `ticker:network` to be explicit:

```
usdt:tron    usdt:eth    usdt:bsc
btc:lightning
```

Run `coins usdt` to see all available networks for a coin.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CYPHERGOAT_API_KEY` | — | **Required.** CypherGoat API key |
| `SIMPLEX_WS_URL` | `ws://localhost:5225` | SimpleX WebSocket address |
| `STATE_DB` | `state.db` | Path to the SQLite state file |

## Project Structure

```
├── bot.py           # Main bot — SimpleX connection, command routing, state management
├── cyphergoat.py    # CypherGoat API client
├── coins.json       # Supported coins and network mappings
├── requirements.txt
├── .env.example
└── state.db         # Runtime state (created automatically, not committed)
```

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
