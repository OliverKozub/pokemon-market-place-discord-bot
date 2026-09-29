"""Echo Labs TCGplayer price bot.

Listens in every channel it can read.

    !price id:509980
    !price name:Charizard ex

The product page at https://www.tcgplayer.com/product/<id> is an empty shell.
Live price, name, and set come from the marketplace API that page calls:

    GET  https://mp-search-api.tcgplayer.com/v1/product/<id>/details
    GET  https://mpapi.tcgplayer.com/v2/product/<id>/pricepoints
    POST https://mp-search-api.tcgplayer.com/v1/search/request?q=<name>

Setup:
    1. In the Discord developer portal, name the application Echo Labs and
       turn on the Message Content intent.
    2. Copy .env.example to .env and set DISCORD_TOKEN.
    3. Invite the bot with View Channel, Send Messages, Embed Links,
       Attach Files, and Read Message History.
    4. python main.py

Check the price lookup without Discord:

    python main.py --check id:509980
    python main.py --check "name:Charizard ex"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import certifi
import discord
from discord.ext import commands
from discord.utils import escape_markdown
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ECHO_PATH = ROOT / "assets" / "echo.png"
ECHO_FILENAME = "echo.png"
# Signed Discord CDN link for the same logo. It expires, so the bot attaches
# assets/echo.png when that file is present.
ECHO_CDN = (
    "https://cdn.discordapp.com/attachments/1442965150968844511/"
    "1442965179909537984/Capture.PNG?ex=6abc160b&is=6abac48b&"
    "hm=80fa809207c3db0428851c3a7248abe637995083737b05c99f9a39e7a8e75253"
)
BOT_NAME = "Echo Labs"
# discord.py drops color 0 because it is falsy, which removes the sidebar.
EMBED_COLOR = 0x111111

DETAILS_URL = "https://mp-search-api.tcgplayer.com/v1/product/{id}/details"
PRICE_URL = "https://mpapi.tcgplayer.com/v2/product/{id}/pricepoints"
SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request"
PRODUCT_URL = "https://www.tcgplayer.com/product/{id}"
IMAGE_URL = "https://tcgplayer-cdn.tcgplayer.com/product/{id}_in_1000x1000.jpg"

ID_RE = re.compile(r"^id:\s*<?(\d+)>?\s*$", re.IGNORECASE)
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.IGNORECASE)

TCG_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://www.tcgplayer.com",
    "Referer": "https://www.tcgplayer.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

log = logging.getLogger("echo")


class ProductNotFound(Exception):
    pass


class TcgError(Exception):
    def __init__(self, status: int):
        super().__init__(f"TCGplayer HTTP {status}")
        self.status = status


@dataclass
class Product:
    product_id: int
    name: str
    set_name: str | None
    rarity: str | None
    number: str | None
    market_price: float | None
    lowest_price: float | None
    lowest_with_shipping: float | None
    listings: int | None
    image_url: str | None
    printings: list[dict]

    @property
    def url(self) -> str:
        return PRODUCT_URL.format(id=self.product_id)


def client_session() -> aiohttp.ClientSession:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.ClientSession(
        headers=TCG_HEADERS,
        timeout=aiohttp.ClientTimeout(total=15),
        connector=aiohttp.TCPConnector(ssl=ssl_context),
    )


def as_int(value) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value) -> float | None:
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def fetch_json(session: aiohttp.ClientSession, method: str, url: str, payload=None):
    try:
        async with session.request(method, url, json=payload) as response:
            body = await response.text()
            if response.status in (400, 404):
                raise ProductNotFound()
            if response.status >= 400:
                log.warning("TCGplayer %s %s -> %s", method, url, response.status)
                raise TcgError(response.status)
            if not body:
                raise TcgError(response.status)
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise TcgError(response.status) from exc
    except aiohttp.ClientError as exc:
        raise TcgError(0) from exc
    except asyncio.TimeoutError as exc:
        raise TcgError(0) from exc


def search_payload(pokemon_only: bool) -> dict:
    term = {"productLineName": ["Pokemon"]} if pokemon_only else {}
    return {
        "algorithm": "sales_synonym_v2",
        "from": 0,
        "size": 5,
        "filters": {"term": term, "range": {}, "match": {}},
        "listingSearch": {
            "context": {"cart": {}},
            "filters": {
                "term": {"sellerStatus": "Live", "channelId": 0},
                "range": {"quantity": {"gte": 1}},
                "exclude": {"channelExclusion": 0},
            },
        },
        "context": {"cart": {}, "shippingCountry": "US", "userProfile": {}},
        "settings": {"useFuzzySearch": True, "didYouMean": {}},
        "sort": {},
    }


def search_hits(payload) -> list[dict]:
    blocks = payload.get("results") if isinstance(payload, dict) else None
    if not blocks:
        return []
    hits = blocks[0].get("results") or []
    seen: set[int] = set()
    unique: list[dict] = []
    for hit in hits:
        product_id = as_int(hit.get("productId"))
        if product_id is None or product_id in seen:
            continue
        seen.add(product_id)
        unique.append(hit)
    return unique


async def search_products(session: aiohttp.ClientSession, name: str) -> list[dict]:
    query = clip(name, 100)
    url = f"{SEARCH_URL}?{urlencode({'q': query, 'isList': 'false'})}"
    for pokemon_only in (True, False):
        payload = await fetch_json(session, "POST", url, search_payload(pokemon_only))
        hits = search_hits(payload)
        if hits:
            return hits
    return []


async def fetch_pricepoints(session: aiohttp.ClientSession, product_id: int) -> list[dict]:
    try:
        payload = await fetch_json(session, "GET", PRICE_URL.format(id=product_id))
    except (ProductNotFound, TcgError):
        log.warning("No price points for product %s", product_id)
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def build_product(details: dict, printings: list[dict]) -> Product:
    product_id = as_int(details.get("productId"))
    name = details.get("productName") or details.get("name")
    if product_id is None or not name:
        raise ProductNotFound()
    attributes = details.get("customAttributes") or {}
    number = attributes.get("number")
    image_count = as_int(details.get("imageCount"))
    image_url = None
    if image_count is None or image_count > 0:
        image_url = IMAGE_URL.format(id=product_id)
    listings = as_int(details.get("listings"))
    if listings is None:
        listings = as_int(details.get("totalListings"))
    return Product(
        product_id=product_id,
        name=str(name),
        set_name=details.get("setName") or None,
        rarity=details.get("rarityName") or None,
        number=str(number) if number else None,
        market_price=as_float(details.get("marketPrice")),
        lowest_price=as_float(details.get("lowestPrice")),
        lowest_with_shipping=as_float(details.get("lowestPriceWithShipping")),
        listings=listings,
        image_url=image_url,
        printings=printings,
    )


async def product_by_id(session: aiohttp.ClientSession, product_id: int) -> Product:
    details = await fetch_json(session, "GET", DETAILS_URL.format(id=product_id))
    if not isinstance(details, dict):
        raise ProductNotFound()
    printings = await fetch_pricepoints(session, product_id)
    return build_product(details, printings)


def echo_file() -> discord.File | None:
    if ECHO_PATH.is_file():
        return discord.File(ECHO_PATH, filename=ECHO_FILENAME)
    return None


def apply_branding(embed: discord.Embed, attached: bool) -> None:
    icon = f"attachment://{ECHO_FILENAME}" if attached else ECHO_CDN
    embed.set_author(name=BOT_NAME, icon_url=icon)
    footer = embed.footer.text if embed.footer and embed.footer.text else BOT_NAME
    embed.set_footer(text=footer, icon_url=icon)


def number_label(number: str | None) -> str | None:
    if not number:
        return None
    text = number.strip()
    if not text:
        return None
    return text if text.startswith("#") else f"#{text}"


def build_price_embed(product: Product, others: list[dict]) -> discord.Embed:
    description_bits = [
        bit
        for bit in (product.set_name, product.rarity, number_label(product.number))
        if bit
    ]
    embed = discord.Embed(
        title=clip(product.name, 256),
        url=product.url,
        description=" · ".join(description_bits) or None,
        color=EMBED_COLOR,
    )
    if product.image_url:
        embed.set_image(url=product.image_url)

    priced = [row for row in product.printings if as_float(row.get("marketPrice")) is not None]
    if priced:
        for row in priced:
            embed.add_field(
                name=f"{row.get('printingType') or 'Market'} market",
                value=money(as_float(row.get("marketPrice"))),
                inline=True,
            )
    else:
        embed.add_field(name="Market", value=money(product.market_price), inline=True)

    embed.add_field(name="Low", value=money(product.lowest_price), inline=True)
    low_ship = product.lowest_with_shipping
    if low_ship is not None and low_ship != product.lowest_price:
        embed.add_field(name="Low + shipping", value=money(low_ship), inline=True)

    medians = [row for row in product.printings if as_float(row.get("listedMedianPrice")) is not None]
    for row in medians:
        label = "Median" if len(medians) == 1 else f"{row.get('printingType') or 'Listed'} median"
        embed.add_field(name=label, value=money(as_float(row.get("listedMedianPrice"))), inline=True)

    if others:
        lines = []
        for hit in others[:4]:
            product_id = as_int(hit.get("productId"))
            if product_id is None:
                continue
            label = escape_markdown(clip(str(hit.get("productName") or "Unknown"), 80))
            set_name = escape_markdown(clip(str(hit.get("setName") or "Unknown set"), 60))
            price = money(as_float(hit.get("marketPrice")))
            lines.append(f"{label} · {set_name} · {price} · `id:{product_id}`")
        if lines:
            embed.add_field(name="Other matches", value="\n".join(lines)[:1024], inline=False)

    footer_bits = [f"id:{product.product_id}"]
    if product.listings is not None:
        footer_bits.append(f"{product.listings} listings")
    embed.set_footer(text=" · ".join(footer_bits))
    apply_branding(embed, attached=ECHO_PATH.is_file())
    return embed


def build_notice(text: str) -> discord.Embed:
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    embed.set_footer(text=BOT_NAME)
    apply_branding(embed, attached=ECHO_PATH.is_file())
    return embed


USAGE = (
    "Look up a TCGplayer price with an id or a name.\n"
    "`!price id:509980`\n"
    "`!price name:Charizard ex`"
)


class EchoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            description="Echo Labs TCGplayer prices",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = client_session()

    async def close(self):
        session = self.session
        if session is not None and not session.closed:
            await session.close()
        await super().close()


bot = EchoBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s. Watching every channel the bot can read.", bot.user)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="TCGplayer prices",
        )
    )


async def send_embed(ctx: commands.Context, embed: discord.Embed) -> None:
    attachment = echo_file()
    kwargs = {"embed": embed, "mention_author": False}
    if attachment is not None:
        kwargs["file"] = attachment
    try:
        await ctx.reply(**kwargs)
    except discord.HTTPException:
        log.exception("Could not send the price embed")


@bot.command(name="price")
@commands.cooldown(1, 3, commands.BucketType.user)
async def price(ctx: commands.Context, *, query: str | None = None):
    """Market price for a TCGplayer product. `!price id:123` or `!price name:card`."""
    text = (query or "").strip()
    id_match = ID_RE.match(text)
    name_match = NAME_RE.match(text)
    if id_match is None and name_match is None:
        await send_embed(ctx, build_notice(USAGE))
        return

    if bot.session is None or bot.session.closed:
        await send_embed(ctx, build_notice("Price lookup is not ready yet. Try again in a moment."))
        return

    async with ctx.typing():
        try:
            others: list[dict] = []
            if id_match is not None:
                product = await product_by_id(bot.session, int(id_match.group(1)))
            else:
                name = name_match.group(1)
                hits = await search_products(bot.session, name)
                if not hits:
                    await send_embed(
                        ctx,
                        build_notice(f"No TCGplayer product matched `{clip(name, 80)}`."),
                    )
                    return
                chosen = as_int(hits[0].get("productId"))
                if chosen is None:
                    raise ProductNotFound()
                product = await product_by_id(bot.session, chosen)
                others = [
                    hit for hit in hits[1:] if as_int(hit.get("productId")) != product.product_id
                ]
        except ProductNotFound:
            await send_embed(ctx, build_notice("No TCGplayer product found for that lookup."))
            return
        except TcgError:
            log.exception("TCGplayer lookup failed")
            await send_embed(
                ctx,
                build_notice("TCGplayer didn't return a price. Try again in a moment."),
            )
            return

    await send_embed(ctx, build_price_embed(product, others))


@price.error
async def price_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(
            f"Wait {error.retry_after:.0f}s before another price check.",
            mention_author=False,
        )
        return
    log.exception("Price command failed", exc_info=error)
    await send_embed(ctx, build_notice("Something went wrong looking up that price."))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if ctx.command and ctx.command.has_error_handler():
        return
    log.exception("Command failed", exc_info=error)


def format_check(product: Product, others: list[dict]) -> str:
    lines = [
        f"{product.name} ({product.url})",
        f"set: {product.set_name or '—'}  rarity: {product.rarity or '—'}  number: {product.number or '—'}",
        f"market: {money(product.market_price)}  low: {money(product.lowest_price)}  "
        f"ship: {money(product.lowest_with_shipping)}  listings: {product.listings if product.listings is not None else '—'}",
        f"image: {product.image_url or '—'}",
    ]
    for row in product.printings:
        market = as_float(row.get("marketPrice"))
        median = as_float(row.get("listedMedianPrice"))
        if market is None and median is None:
            continue
        lines.append(
            f"  {row.get('printingType')}: market {money(market)}  median {money(median)}"
        )
    for hit in others[:4]:
        lines.append(
            f"other: {hit.get('productName')} | {hit.get('setName')} | "
            f"{money(as_float(hit.get('marketPrice')))} | id:{as_int(hit.get('productId'))}"
        )
    return "\n".join(lines)


async def check(query: str) -> int:
    id_match = ID_RE.match(query.strip())
    name_match = NAME_RE.match(query.strip())
    if id_match is None and name_match is None:
        print(USAGE)
        return 2
    async with client_session() as session:
        try:
            others: list[dict] = []
            if id_match is not None:
                product = await product_by_id(session, int(id_match.group(1)))
            else:
                hits = await search_products(session, name_match.group(1))
                if not hits:
                    print("No product matched.")
                    return 1
                product = await product_by_id(session, int(hits[0]["productId"]))
                others = hits[1:]
        except ProductNotFound:
            print("No product found.")
            return 1
        except TcgError as exc:
            print(f"TCGplayer request failed ({exc.status}).")
            return 1
    print(format_check(product, others))
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        query = " ".join(sys.argv[2:]).strip()
        if len(query) >= 2 and query[0] == query[-1] and query[0] in {"'", '"'}:
            query = query[1:-1]
        raise SystemExit(asyncio.run(check(query)))

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in the environment or in .env.")
    bot.run(token)


if __name__ == "__main__":
    main()
