#!/usr/bin/env python3
"""CypherGoat SimpleX chat bot — lets users swap crypto via CypherGoat."""

import asyncio
import json
import logging
import os
import pathlib
import sqlite3
import time
import traceback
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
from dotenv import load_dotenv

from cyphergoat import CypherGoatClient, CypherGoatError

_log_level = logging.DEBUG if os.getenv("BOT_DEBUG") else logging.INFO
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)

load_dotenv()

SIMPLEX_WS_URL = os.getenv("SIMPLEX_WS_URL", "ws://localhost:5225")
API_KEY = os.getenv("CYPHERGOAT_API_KEY", "")
DB_PATH = os.getenv("STATE_DB", "state.db")

PENDING_SWAP_TIMEOUT = 10 * 60   # seconds before an unanswered rate list expires
RATE_LIMIT_CALLS = 10             # max API-hitting commands per contact
RATE_LIMIT_WINDOW = 60            # per this many seconds

COINS_FILE = pathlib.Path(__file__).parent / "coins.json"
with open(COINS_FILE) as f:
    ALL_COINS: list[dict] = json.load(f)


BLOCKED_EXCHANGES: set[str] = {
    "stealthex",
    "changenow",
    "fixedfloat",
    "simpleswap",
    "exolix",
    "nanswap",
}


def is_blocked(exchange: str) -> bool:
    return exchange.lower() in BLOCKED_EXCHANGES

# HELP_TEXT is an alias — both commands show the same content
HELP_TEXT = None  # assigned after WELCOME_TEXT is defined


# -- Coin helpers -------------------------------------------------------------

def resolve_coin(token: str) -> Optional[tuple[str, str]]:
    """Parse 'ticker' or 'ticker:network' and return (ticker, network) or None."""
    if ":" in token:
        ticker, network = token.lower().split(":", 1)
    else:
        ticker = token.lower()
        network = ticker  # default: network == ticker

    for coin in ALL_COINS:
        if coin["ticker"].lower() == ticker and coin["network"].lower() == network:
            return ticker, network

    # Fallback: any coin with that ticker
    for coin in ALL_COINS:
        if coin["ticker"].lower() == ticker:
            return coin["ticker"].lower(), coin["network"].lower()

    return None


def coin_display(ticker: str, network: str) -> str:
    for coin in ALL_COINS:
        if coin["ticker"].lower() == ticker and coin["network"].lower() == network:
            name = coin["name"]
            if ticker == network:
                return f"{name} ({ticker.upper()})"
            return f"{name} ({ticker.upper()} on {network.upper()})"
    return f"{ticker.upper()}:{network.upper()}"


# -- Conversation state -------------------------------------------------------

@dataclass
class PendingEstimate:
    coin1: str
    network1: str
    coin2: str
    network2: str
    amount: float
    rates: list[dict]  # list of {Exchange, Amount, KYCScore}


@dataclass
class PendingSwap:
    coin1: str
    network1: str
    coin2: str
    network2: str
    amount: float
    address: str
    rates: list[dict]  # list of {Exchange, Amount, KYCScore}
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > PENDING_SWAP_TIMEOUT


@dataclass
class ContactState:
    name: str = ""
    pending_estimate: Optional[PendingEstimate] = None
    pending_swap: Optional[PendingSwap] = None


# -- Persistence --------------------------------------------------------------

class StateDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_swaps (
                contact_id   INTEGER PRIMARY KEY,
                contact_name TEXT    NOT NULL,
                data         TEXT    NOT NULL,
                created_at   REAL    NOT NULL
            )
        """)
        self.conn.commit()

    def save(self, contact_id: int, contact_name: str, swap: PendingSwap):
        data = json.dumps({
            "coin1": swap.coin1, "network1": swap.network1,
            "coin2": swap.coin2, "network2": swap.network2,
            "amount": swap.amount, "address": swap.address,
            "rates": swap.rates, "created_at": swap.created_at,
        })
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_swaps VALUES (?, ?, ?, ?)",
            (contact_id, contact_name, data, swap.created_at),
        )
        self.conn.commit()

    def delete(self, contact_id: int):
        self.conn.execute("DELETE FROM pending_swaps WHERE contact_id = ?", (contact_id,))
        self.conn.commit()

    def load_all(self) -> list[tuple[int, str, PendingSwap]]:
        rows = self.conn.execute(
            "SELECT contact_id, contact_name, data FROM pending_swaps"
        ).fetchall()
        result = []
        for contact_id, contact_name, data in rows:
            d = json.loads(data)
            swap = PendingSwap(**{k: d[k] for k in (
                "coin1", "network1", "coin2", "network2",
                "amount", "address", "rates", "created_at",
            )})
            result.append((contact_id, contact_name, swap))
        return result

    def close(self):
        self.conn.close()


# -- Rate limiting ------------------------------------------------------------

_rate_buckets: dict[int, deque] = defaultdict(deque)


def is_rate_limited(contact_id: int) -> bool:
    now = time.time()
    bucket = _rate_buckets[contact_id]
    while bucket and bucket[0] < now - RATE_LIMIT_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_CALLS:
        return True
    bucket.append(now)
    return False


# -- Command handlers ---------------------------------------------------------

async def handle_estimate(args: list[str], cg: CypherGoatClient, state: ContactState) -> tuple[str, Optional[PendingEstimate]]:
    if len(args) < 3:
        return "Usage: estimate <amount> <from> <to>\nExample: estimate 0.1 btc xmr", None

    try:
        amount = float(args[0])
    except ValueError:
        return f"Invalid amount: {args[0]}", None

    from_resolved = resolve_coin(args[1])
    to_resolved = resolve_coin(args[2])

    if not from_resolved:
        return f"Unknown coin: {args[1]}\nTip: use 'coins {args[1]}' to search.", None
    if not to_resolved:
        return f"Unknown coin: {args[2]}\nTip: use 'coins {args[2]}' to search.", None

    coin1, network1 = from_resolved
    coin2, network2 = to_resolved

    try:
        result = await cg.estimate(coin1, network1, coin2, network2, amount)
    except CypherGoatError as e:
        return f"Error getting estimate: {e}", None

    rates = [r for r in result.get("rates", {}).get("Results", []) if not is_blocked(r.get("Exchange", ""))]
    min_amount = result.get("min", 0)

    if not rates:
        msg = f"No rates available for {coin_display(coin1, network1)} → {coin_display(coin2, network2)}."
        if min_amount:
            msg += f"\nMinimum amount: {min_amount}"
        return msg, None

    pending = PendingEstimate(
        coin1=coin1, network1=network1,
        coin2=coin2, network2=network2,
        amount=amount, rates=rates,
    )

    header = f"*Rates* — {amount} {coin_display(coin1, network1)} → {coin_display(coin2, network2)}"
    if min_amount:
        header += f"\nMinimum: {min_amount}"

    lines = [header, ""]
    for i, r in enumerate(rates, 1):
        kyc = r.get("KYCScore", "?")
        lines.append(f"{i:>2}. {r['Exchange']:<16} {r['Amount']:.6g} {coin2.upper()}  [KYC {kyc}]")

    lines += ["", "Reply with a number to swap, or `cancel` to abort."]
    return "\n".join(lines), pending


def _format_swap_created(tx: dict, coin1: str, coin2: str, partner: str) -> str:
    t = tx.get("transaction", tx)
    cgid = t.get("CGID", "")
    lines = [
        "*Swap Created*",
        "",
        f"Send   {t.get('SendAmount', '?')} {coin1.upper()}",
        f"To     {t.get('Address', 'N/A')}",
    ]
    if t.get("Memo"):
        lines.append(f"Memo   {t['Memo']}")
    lines += [
        "",
        f"Receive  ~{t.get('EstimateAmount', '?')} {coin2.upper()}",
        f"Via      {t.get('Provider', partner)}",
        f"CGID     {cgid}",
        "",
        f"Track: `track {cgid}`",
    ]
    if t.get("Track"):
        lines.append(t["Track"])
    return "\n".join(lines)


async def handle_swap(
    args: list[str], cg: CypherGoatClient, state: ContactState
) -> tuple[str, Optional[PendingSwap]]:
    """
    4 args: swap <amount> <from> <to> <address>  → fetch estimates, ask user to pick
    5 args: swap <amount> <from> <to> <exchange> <address>  → execute immediately
    """
    if len(args) < 4:
        return (
            "Usage:\n"
            "  swap <amount> <from> <to> <address>\n"
            "    (shows rates, you pick the exchange)\n"
            "  swap <amount> <from> <to> <exchange> <address>\n"
            "    (execute directly with a specific exchange)\n"
            "Example: swap 0.1 btc xmr <your_xmr_address>"
        ), None

    try:
        amount = float(args[0])
    except ValueError:
        return f"Invalid amount: {args[0]}", None

    from_resolved = resolve_coin(args[1])
    to_resolved = resolve_coin(args[2])

    if not from_resolved:
        return f"Unknown coin: {args[1]}\nTip: use 'coins {args[1]}' to search.", None
    if not to_resolved:
        return f"Unknown coin: {args[2]}\nTip: use 'coins {args[2]}' to search.", None

    coin1, network1 = from_resolved
    coin2, network2 = to_resolved

    if len(args) == 4:
        # Interactive flow: fetch estimates, let user pick
        address = args[3]

        try:
            result = await cg.estimate(coin1, network1, coin2, network2, amount)
        except CypherGoatError as e:
            return f"Error fetching rates: {e}", None

        rates = [r for r in result.get("rates", {}).get("Results", []) if not is_blocked(r.get("Exchange", ""))]
        min_amount = result.get("min", 0)

        if not rates:
            msg = f"No rates available for {coin_display(coin1, network1)} → {coin_display(coin2, network2)}."
            if min_amount:
                msg += f"\nMinimum amount: {min_amount}"
            return msg, None

        pending = PendingSwap(
            coin1=coin1, network1=network1,
            coin2=coin2, network2=network2,
            amount=amount, address=address,
            rates=rates,
        )

        header = f"*Rates* — {amount} {coin_display(coin1, network1)} → {coin_display(coin2, network2)}"
        if min_amount:
            header += f"\nMinimum: {min_amount}"

        lines = [header, ""]
        for i, r in enumerate(rates, 1):
            kyc = KYC_LABELS.get(r.get("KYCScore", 0), "?")
            lines.append(f"{i:>2}. {r['Exchange']:<16} {r['Amount']:.6g} {coin2.upper()}  [{kyc}]")

        lines += ["", "Reply with a number to swap, or `cancel` to abort."]
        return "\n".join(lines), pending

    else:
        # Direct execution: swap <amount> <from> <to> <exchange> <address>
        partner = args[3]
        address = args[4]

        if is_blocked(partner):
            return f"Exchange '{partner}' is not available.", None

        try:
            tx = await cg.swap(coin1, network1, coin2, network2, amount, partner, address)
        except CypherGoatError as e:
            return f"Swap failed: {e}", None

        return _format_swap_created(tx, coin1, coin2, partner), None


async def handle_swap_selection(
    choice: str, cg: CypherGoatClient, state: ContactState
) -> Optional[str]:
    """Handle a numeric reply when a PendingSwap is waiting. Returns reply or None if not applicable."""
    if state.pending_swap is None:
        return None

    if choice.lower() == "cancel":
        state.pending_swap = None
        return "Swap cancelled."

    try:
        idx = int(choice)
    except ValueError:
        return None  # not a selection, let normal command handling proceed

    pending = state.pending_swap
    if idx < 1 or idx > len(pending.rates):
        return f"Please reply with a number between 1 and {len(pending.rates)}, or 'cancel'."

    partner = pending.rates[idx - 1]["Exchange"]
    state.pending_swap = None

    try:
        tx = await cg.swap(
            pending.coin1, pending.network1,
            pending.coin2, pending.network2,
            pending.amount, partner, pending.address,
        )
    except CypherGoatError as e:
        return f"Swap failed: {e}"

    return _format_swap_created(tx, pending.coin1, pending.coin2, partner)


async def handle_track(args: list[str], cg: CypherGoatClient) -> str:
    if not args:
        return "Usage: track <cgid>\nExample: track abc123"

    cgid = args[0]
    try:
        tx = await cg.transaction(cgid)
    except CypherGoatError as e:
        return f"Error: {e}"

    t = tx.get("transaction", tx)
    status = t.get("Status") or "pending"
    done = t.get("Done", False)
    coin1 = t.get("Coin1", "?").upper()
    coin2 = t.get("Coin2", "?").upper()
    lines = [
        f"*Transaction* `{cgid}`",
        "",
        f"Status   {status}{' ✓' if done else ''}",
        f"Pair     {coin1} → {coin2}",
        f"Send     {t.get('SendAmount', '?')} {coin1}",
        f"Receive  ~{t.get('EstimateAmount', '?')} {coin2}",
        f"Via      {t.get('Provider', '?')}",
    ]
    if t.get("Track"):
        lines += ["", t["Track"]]
    return "\n".join(lines)


def handle_coins(args: list[str]) -> str:
    query = args[0].lower() if args else ""
    matched = [
        c for c in ALL_COINS
        if not query or query in c["ticker"].lower() or query in c["name"].lower()
    ]

    if not matched:
        return f"No coins found matching '{query}'.\nTry: `coins btc` or `coins usdt`"

    seen: dict[str, tuple[str, list[str]]] = {}
    for c in matched:
        ticker = c["ticker"].upper()
        name = c["name"]
        net = c["network"].upper()
        if ticker not in seen:
            seen[ticker] = (name, [])
        seen[ticker][1].append(net)

    lines = ["*Supported Coins*" + (f" matching '{query}'" if query else ""), ""]
    for ticker, (name, networks) in sorted(seen.items()):
        nets = ", ".join(networks)
        if len(networks) == 1 and networks[0].lower() == ticker.lower():
            lines.append(f"{ticker:<8}  {name}")
        else:
            lines.append(f"{ticker:<8}  {name}  [{nets}]")

    if len(lines) > 52:
        lines = lines[:52]
        lines.append("  ... use `coins <search>` to filter")

    lines += ["", "Use `coin:network` syntax, e.g. `usdt:tron`"]
    return "\n".join(lines)


# -- SimpleX messaging --------------------------------------------------------

async def send_message(ws, contact_name: str, text: str):
    corr_id = str(uuid.uuid4())[:8]
    name_ref = f'"{contact_name}"' if " " in contact_name else contact_name
    cmd = f"@{name_ref} {text}"
    payload = json.dumps({"corrId": corr_id, "cmd": cmd})
    await ws.send(payload)


async def accept_contact_request(ws, req_id: int, contact_name: str):
    corr_id = str(uuid.uuid4())[:8]
    name_ref = f'"{contact_name}"' if " " in contact_name else contact_name
    cmd = f"/ac {name_ref}"
    payload = json.dumps({"corrId": corr_id, "cmd": cmd})
    await ws.send(payload)


WELCOME_TEXT = """\
*CypherGoat Swap Bot*
Swap crypto privately across 20+ exchanges.

*Commands*

`estimate 0.1 btc xmr`
  Compare rates across all exchanges.

`swap 0.1 btc xmr <your_address>`
  See rates and pick an exchange interactively.

`swap 0.1 btc xmr QuickEx <your_address>`
  Swap directly with a specific exchange.

`track <cgid>`
  Check the status of a swap.

`coins usdt`
  List supported coins and their networks.

*Multi-network coins*
Some coins run on multiple chains. Use `coin:network`:
  usdt:tron   usdt:eth   btc:lightning

Type `help` to see this message again."""

HELP_TEXT = WELCOME_TEXT


async def _send_welcome(ws, contact_name: str):
    await send_message(ws, contact_name, WELCOME_TEXT)
    log.info("Sent welcome to %r", contact_name)


def extract_contact_ready(event: dict) -> Optional[tuple[Optional[int], str]]:
    """Return contact info when a contact can receive direct messages."""
    resp = event.get("resp", {})
    evt_type = resp.get("type")
    if evt_type not in ("contactConnected", "contactSndReady", "acceptingContactRequest"):
        return None
    contact = resp.get("contact", {})
    name = contact.get("localDisplayName", "unknown")
    return contact.get("contactId"), name


def extract_contact_request(event: dict) -> Optional[tuple[int, str]]:
    """Return (contactRequestId, displayName) if this is an incoming contact request."""
    resp = event.get("resp", {})
    # SimpleX uses "contactRequest" in newer versions, "receivedContactRequest" in older ones
    if resp.get("type") not in ("contactRequest", "receivedContactRequest"):
        return None
    req = resp.get("contactRequest", {})
    return req.get("contactRequestId"), req.get("localDisplayName", "unknown")


def extract_pending_contact_requests(event: dict) -> list[tuple[int, str]]:
    """Return [(contactRequestId, displayName)] from request-list style responses."""
    resp = event.get("resp", {})
    results = []
    reqs = resp.get("contactRequests")
    if not isinstance(reqs, list):
        reqs = resp.get("userContactRequests")
    if not isinstance(reqs, list):
        return []
    for req in reqs:
        req_id = req.get("contactRequestId")
        name = req.get("localDisplayName", "unknown")
        if req_id is not None:
            results.append((req_id, name))
    return results


def extract_cmd_error(event: dict) -> Optional[str]:
    """Return a CLI command error message if present."""
    resp = event.get("resp", {})
    if resp.get("type") != "chatCmdError":
        return None
    chat_error = resp.get("chatError", {})
    error_type = chat_error.get("errorType", {})
    return error_type.get("message") or chat_error.get("message") or "unknown command error"


def extract_messages(event: dict) -> list[tuple[int, str, str]]:
    """Return list of (contact_id, contact_name, text) from a newChatItems event."""
    resp = event.get("resp", {})
    if resp.get("type") != "newChatItems":
        return []

    results = []
    for chat_item in resp.get("chatItems", []):
        chat_info = chat_item.get("chatInfo", {})

        if chat_info.get("type") != "direct":
            continue

        contact = chat_info.get("contact", {})
        contact_id = contact.get("contactId")
        contact_name = contact.get("localDisplayName", "unknown")

        item = chat_item.get("chatItem", {})
        content = item.get("content", {})

        if content.get("type") != "rcvMsgContent":
            continue

        msg_content = content.get("msgContent", {})
        if msg_content.get("type") != "text":
            continue

        text = msg_content.get("text", "").strip()
        if text:
            results.append((contact_id, contact_name, text))

    return results


async def process_message(ws, contact_id: int, contact_name: str, text: str,
                          cg: CypherGoatClient, states: dict, db: StateDB):
    try:
        state = states.setdefault(contact_id, ContactState())
        state.name = contact_name
        parts = text.split()
        if not parts:
            return

        # Expire stale pending swap before doing anything
        if state.pending_swap is not None and state.pending_swap.is_expired():
            state.pending_swap = None
            db.delete(contact_id)
            await send_message(ws, contact_name,
                "Your swap selection expired (10 min timeout). "
                "Run `swap` again to get fresh rates.")
            return

        # Check if user is selecting an exchange from a pending swap
        if state.pending_swap is not None:
            selection_reply = await handle_swap_selection(parts[0], cg, state)
            if selection_reply is not None:
                db.delete(contact_id)
                await send_message(ws, contact_name, selection_reply)
                log.info("[%s] %r → swap selection replied", contact_name, text)
                return

        cmd = parts[0].lower()
        args = parts[1:]

        # Rate-limit API-hitting commands
        if cmd in ("estimate", "swap") and is_rate_limited(contact_id):
            await send_message(ws, contact_name,
                f"Slow down — max {RATE_LIMIT_CALLS} requests per {RATE_LIMIT_WINDOW}s.")
            return

        if cmd in ("help", "/help", "start"):
            reply = WELCOME_TEXT
        elif cmd == "estimate":
            reply, pending = await handle_estimate(args, cg, state)
            if pending:
                state.pending_estimate = pending
        elif cmd == "swap":
            reply, pending_swap = await handle_swap(args, cg, state)
            if pending_swap:
                state.pending_swap = pending_swap
                db.save(contact_id, contact_name, pending_swap)
        elif cmd == "track":
            reply = await handle_track(args, cg)
        elif cmd == "coins":
            reply = handle_coins(args)
        elif cmd == "cancel":
            if state.pending_swap:
                state.pending_swap = None
                db.delete(contact_id)
                reply = "Swap cancelled."
            else:
                reply = "Nothing to cancel."
        else:
            reply = f"Unknown command: {cmd}\nSend `help` for available commands."

        await send_message(ws, contact_name, reply)
        log.info("[%s] %r → replied", contact_name, text)

    except Exception as e:
        log.error("Error processing message from %r: %s", contact_name, e, exc_info=True)
        try:
            await send_message(ws, contact_name, f"Internal error: {e}")
        except Exception:
            pass


# -- Main loop ----------------------------------------------------------------

async def _notify_interrupted(ws: object, contact_name: str):
    """Tell a user their mid-swap session was interrupted by a bot restart."""
    await asyncio.sleep(2)  # give the WS connection a moment to settle
    await send_message(ws, contact_name,
        "The bot restarted while you had a pending swap selection. "
        "Please run `swap` again to get fresh rates.")


async def run_bot():
    if not API_KEY:
        log.warning("CYPHERGOAT_API_KEY not set — swap/estimate commands will fail")

    db = StateDB(DB_PATH)
    cg = CypherGoatClient(API_KEY)
    states: dict[int, ContactState] = {}
    welcomed_contacts: set[str] = set()

    # Restore any pending swaps that survived a previous crash
    for contact_id, contact_name, swap in db.load_all():
        db.delete(contact_id)  # always clear — rates are stale after restart
        state = states.setdefault(contact_id, ContactState())
        state.name = contact_name
        log.info("Cleared stale pending swap for %r (id=%d)", contact_name, contact_id)

    log.info("Connecting to SimpleX at %s ...", SIMPLEX_WS_URL)

    try:
        async for ws in websockets.connect(SIMPLEX_WS_URL, ping_interval=30):
            try:
                log.info("Connected. Bot is running.")

                # Verify connection and log active user
                await ws.send(json.dumps({"corrId": "init-user", "cmd": "/user"}))

                # Notify users whose swap was interrupted before this connection
                for contact_id, state in states.items():
                    if state.name:
                        asyncio.create_task(_notify_interrupted(ws, state.name))

                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    evt_type = event.get("resp", {}).get("type")
                    if evt_type and any(k in evt_type for k in ("contact", "Contact", "request", "Request")):
                        log.info("Contact event: type=%s", evt_type)
                    else:
                        log.debug("Event: type=%s", evt_type)

                    cmd_error = extract_cmd_error(event)
                    if cmd_error is not None:
                        log.error("SimpleX command error: %s", cmd_error)
                        continue

                    # Auto-accept contact requests (live and from /requests on startup)
                    pending_reqs = extract_pending_contact_requests(event)
                    for req_id, req_name in pending_reqs:
                        await accept_contact_request(ws, req_id, req_name)
                        log.info("Auto-accepted pending request from %r (id=%s)", req_name, req_id)

                    req = extract_contact_request(event)
                    if req is not None:
                        req_id, req_name = req
                        await accept_contact_request(ws, req_id, req_name)
                        log.info("Auto-accepted contact request from %r (id=%s)", req_name, req_id)
                        continue

                    if pending_reqs:
                        continue

                    # Send welcome once the contact can receive direct messages
                    connected = extract_contact_ready(event)
                    if connected is not None:
                        _, conn_name = connected
                        if conn_name not in welcomed_contacts:
                            welcomed_contacts.add(conn_name)
                            asyncio.create_task(_send_welcome(ws, conn_name))
                        continue

                    # Handle incoming messages
                    for contact_id, contact_name, text in extract_messages(event):
                        asyncio.create_task(
                            process_message(ws, contact_id, contact_name, text, cg, states, db)
                        )

            except websockets.ConnectionClosed as e:
                log.warning("Connection closed (%s), reconnecting in 5s ...", e)
                await asyncio.sleep(5)
            except Exception:
                log.exception("Unexpected error, reconnecting in 5s ...")
                await asyncio.sleep(5)
    finally:
        await cg.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
