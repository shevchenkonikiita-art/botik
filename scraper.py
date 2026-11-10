"""Парсери оголошень: OLX + Otodom + Facebook Marketplace (Poznań)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote_plus, urlencode, urljoin

from playwright.sync_api import sync_playwright

from config import CITY_SLUG, DEFAULT_FILTERS, DISTRICTS, WATCH_MAX_CARD_SCAN
from price import PriceBreakdown, analyze_price, fits_budget

logger = logging.getLogger(__name__)

OLX_BASE = "https://www.olx.pl"
OTODOM_BASE = "https://www.otodom.pl"
FB_MARKET_URL = (
    "https://www.facebook.com/marketplace/poznan/search"
    "?query=mieszkanie%20wynajem&exact=false"
)

NAJEM_RE = re.compile(r"okazjonaln", re.IGNORECASE)
PRIVATE_HINTS = ("prywatn", "bez pośrednik", "bezposrednio", "bezpośrednio", "od właściciela")
AGENCY_HINTS = ("biuro", "agency", "pośrednik", "posrednik", "oferta biura")
ROOMS_RE = re.compile(r"(\d+)\s*(?:pokoj|pok\.|room)", re.IGNORECASE)
_TITLE_SKIP = re.compile(
    r"^(dodane|dzisiaj|dzis|wczoraj|odświe|odswie|today|yesterday|\d+\s+(?:minut|godzin))",
    re.IGNORECASE,
)
WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def _as_warsaw(dt: datetime) -> datetime:
    """Усі порівняння дат — у Europe/Warsaw."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=WARSAW_TZ)
    return dt.astimezone(WARSAW_TZ)


@dataclass
class Listing:
    id: str
    title: str
    price: str
    location: str
    area: str
    url: str
    source: str = "olx"
    photo_url: str | None = None
    raw_text: str = ""
    owner_hint: str = "unknown"
    pets_hint: str = "unknown"
    najem_hint: bool = False
    rooms_hint: int | None = None
    price_info: PriceBreakdown | None = None
    photos: list[str] = field(default_factory=list)
    rent_zl: int | None = None
    czynsz_zl: int | None = None
    area_m2: float | None = None
    furnished: str | None = None  # tak | nie
    posted_label: str = ""
    district: str = ""
    street: str = ""
    details_loaded: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def maps_url(self) -> str | None:
        if self.street:
            q = f"{self.street}, Poznań"
            return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)
        district = detect_district(
            self.title, self.location, self.district, self.raw_text[:800]
        )
        if district:
            q = f"{district}, Poznań"
            return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)
        return None

    def _money(self, value: int) -> str:
        return f"{value:,}".replace(",", " ")

    def format_caption(self, *, embed_links: bool = False, display_tz: str | None = None) -> str:
        """Картка 1:1 як у Warsaw Rent (HTML)."""
        import html as html_lib

        title = html_lib.escape(self.title)
        district = detect_district(
            self.title, self.location, self.district, self.raw_text[:800]
        )
        maps = self.maps_url()
        rent = self.rent_zl
        czynsz = self.czynsz_zl if self.czynsz_zl is not None else 0
        if rent is None and self.price_info and self.price_info.base:
            rent = self.price_info.base
        if rent is None:
            price_line = "💰 ціна не вказана"
            total = None
        else:
            total = rent + (czynsz or 0)
            price_line = (
                f"💰 {self._money(rent)} zł + czynsz {self._money(czynsz)} zł "
                f"= {self._money(total)} zł"
            )
            if self.area_m2 and self.area_m2 > 0 and total:
                per = round(total / self.area_m2)
                price_line += f" · ≈ {per} zł/m²"

        area_bits = []
        if self.area_m2:
            area_txt = (
                str(int(self.area_m2))
                if float(self.area_m2).is_integer()
                else str(self.area_m2).replace(".", ",")
            )
            area_bits.append(f"{area_txt} m²")
        rooms = self.rooms_hint
        if rooms:
            area_bits.append(f"{rooms} кімн.")

        owner = "власник" if self.owner_hint != "agency" else "агентство"
        if self.furnished == "tak":
            furn = "🛋 з меблями"
        elif self.furnished == "nie":
            furn = "без меблів"
        else:
            furn = ""
        who = f"👤 {owner}" + (f" · {furn}" if furn else "")

        show_time = bool(self.extras.get("posted_has_time", True))
        if self.extras.get("posted_iso"):
            try:
                posted = format_posted_dt(
                    datetime.fromisoformat(self.extras["posted_iso"]),
                    display_tz,
                    show_time=show_time,
                )
            except ValueError:
                posted = self.posted_label or describe_posted(self.location)
        else:
            posted = self.posted_label or describe_posted(self.location)

        lines = [title, "", price_line]
        if self.street:
            loc = html_lib.escape(self.street)
            if district and district.lower() not in self.street.lower():
                loc += f" · {html_lib.escape(district)}"
            lines.append(f"📍 {loc}")
        elif district:
            lines.append(f"📍 {html_lib.escape(district)}")
        if area_bits:
            lines.append("📐 " + " · ".join(area_bits))
        lines.append(who)
        if posted:
            lines.append(f"🕐 {html_lib.escape(posted)}")
        if embed_links:
            links = [
                f'<a href="{html_lib.escape(self.url)}">🔗 відкрити оголошення</a>',
            ]
            if maps:
                map_label = html_lib.escape(self.street or "вулиця на карті")
                links.append(f'<a href="{html_lib.escape(maps)}">🗺 {map_label}</a>')
            lines.append(" · ".join(links))
        else:
            lines.append(
                f'<a href="{html_lib.escape(self.url)}">відкрити оголошення</a>'
            )
        return "\n".join(lines)

    def format_message(self) -> str:
        return self.format_caption()


def _room_params(rooms: list[int]) -> dict[str, str]:
    room_map = {1: "one", 2: "two", 3: "three", 4: "four"}
    params: dict[str, str] = {}
    values = sorted(set(rooms)) if rooms else [1, 2, 3, 4]
    for i, n in enumerate(values):
        key = room_map.get(n)
        if key:
            params[f"search[filter_enum_rooms][{i}]"] = key
    return params


def _build_olx_url(filters: dict[str, Any], page_num: int = 1) -> str:
    max_price = int(filters.get("max_price") or 99999)
    min_price = int(filters.get("min_price") or 0)
    params: dict[str, Any] = {
        "search[filter_float_m:from]": filters["min_area"],
        "search[filter_float_m:to]": filters["max_area"],
        "search[order]": "created_at:desc",
    }
    if min_price > 0:
        params["search[filter_float_price:from]"] = max(0, min_price - 800)
    if max_price < 90000:
        params["search[filter_float_price:to]"] = max_price
    params.update(_room_params(list(filters.get("rooms") or [])))

    owner = filters.get("owner_type", "all")
    if owner == "private":
        params["search[private_business]"] = "private"
    elif owner == "agency":
        params["search[private_business]"] = "business"

    pets = filters.get("pets", "all")
    if pets == "yes":
        params["search[filter_enum_pets][0]"] = "yes"
    elif pets == "no":
        params["search[filter_enum_pets][0]"] = "no"

    if page_num > 1:
        params["page"] = page_num
    return (
        f"{OLX_BASE}/nieruchomosci/mieszkania/wynajem/{CITY_SLUG}/"
        f"?{urlencode(params)}"
    )


def _build_otodom_url(filters: dict[str, Any], page_num: int = 1) -> str:
    max_price = int(filters.get("max_price") or 99999)
    min_price = int(filters.get("min_price") or 0)
    params: dict[str, Any] = {
        "limit": 36,
        "ownerTypeSingleSelect": "ALL",
        "by": "LATEST",
        "direction": "DESC",
        "viewType": "listing",
    }
    if min_price > 0:
        params["priceMin"] = max(0, min_price - 800)
    if max_price < 90000:
        params["priceMax"] = max_price

    rooms = list(filters.get("rooms") or [])
    if rooms:
        # Otodom: roomsNumber = ONE, TWO, THREE, FOUR
        room_map = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR"}
        for i, n in enumerate(sorted(set(rooms))):
            key = room_map.get(n)
            if key:
                params[f"roomsNumber[{i}]"] = key

    if page_num > 1:
        params["page"] = page_num
    return (
        f"{OTODOM_BASE}/pl/wyniki/wynajem/mieszkanie/wielkopolskie/poznan/poznan"
        f"?{urlencode(params)}"
    )


def _accept_cookies(page) -> None:
    for sel in (
        "#onetrust-accept-btn-handler",
        'button:has-text("Akceptuję")',
        'button:has-text("Accept all")',
        'button:has-text("Zgadzam się")',
        'button:has-text("Allow all cookies")',
        '[data-testid="accept-cookies"]',
        'div[aria-label="Allow all cookies"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(600)
                return
        except Exception:
            continue


def _infer_owner(text: str) -> str:
    low = text.lower()
    if any(h in low for h in AGENCY_HINTS):
        return "agency"
    if any(h in low for h in PRIVATE_HINTS):
        return "private"
    return "unknown"


def _infer_pets(text: str) -> str:
    low = text.lower()
    if any(
        x in low
        for x in (
            "zwierzęta: tak",
            "pets friendly",
            "zwierzęta dozwolone",
            "akceptuję zwierzęta",
        )
    ):
        return "yes"
    if any(
        x in low
        for x in ("zwierzęta: nie", "bez zwierząt", "nie akceptuję zwierząt")
    ):
        return "no"
    return "unknown"


def _infer_rooms(text: str) -> int | None:
    low = text.lower()
    if "kawalerka" in low or "studio" in low:
        return 1
    match = ROOMS_RE.search(text)
    if match:
        return min(int(match.group(1)), 4)
    for n, words in (
        (1, ("1 pok", "1-pok", "jednopokoj")),
        (2, ("2 pok", "2-pok", "dwupokoj")),
        (3, ("3 pok", "3-pok", "trzypokoj")),
        (4, ("4 pok", "4-pok", "czteropokoj")),
    ):
        if any(w in low for w in words):
            return n
    return None


def _stamp_posted_from_card(listing: Listing, text: str) -> None:
    """Дата з картки списку (до відкриття detail) — лише «Dodane …», не весь опис."""
    if listing.extras.get("posted_iso"):
        return
    dodane = re.search(r"Dodane\s+([^\n]{3,80})", text, re.IGNORECASE)
    snippet = dodane.group(1) if dodane else ""
    moment = parse_pl_datetime(snippet) if snippet else None
    if moment:
        moment = _plausible_posted(moment)
    if moment:
        listing.extras["posted_iso"] = moment.isoformat()
        listing.posted_label = format_posted_dt(moment)
    elif snippet:
        label = describe_posted(snippet)
        if label:
            listing.posted_label = label


def _enrich(listing: Listing) -> Listing:
    listing.district = (
        detect_district(listing.title, listing.location, listing.raw_text[:1200])
        or listing.district
    )
    if listing.district and _NOT_DISTRICT.search(listing.district):
        listing.district = detect_district(listing.title, listing.raw_text[:1200])
    listing.price_info = analyze_price(listing.price, listing.raw_text)
    listing.owner_hint = _infer_owner(listing.raw_text)
    listing.pets_hint = _infer_pets(listing.raw_text)
    listing.najem_hint = bool(NAJEM_RE.search(listing.raw_text))
    listing.rooms_hint = _infer_rooms(listing.raw_text)
    return listing


def _best_image_url(card) -> str | None:
    """Бере найбільше фото з src/srcset (Telegram любить нормальний JPG)."""
    img = card.locator("img").first
    if not img.count():
        return None
    srcset = img.get_attribute("srcset") or ""
    src = img.get_attribute("src") or ""
    candidates: list[tuple[int, str]] = []
    if srcset:
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split()
            url = bits[0]
            width = 0
            if len(bits) > 1 and bits[1].endswith("w"):
                try:
                    width = int(bits[1][:-1])
                except ValueError:
                    width = 0
            candidates.append((width, url))
    if src:
        candidates.append((0, src))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    url = candidates[0][1]
    # OLX часто дає ;s=216x152 — підміняємо на більше
    url = re.sub(r";s=\d+x\d+", ";s=1000x700", url)
    return url


def _pick_listing_title(lines: list[str]) -> str:
    for ln in lines:
        t = ln.strip()
        if len(t) < 10 or _TITLE_SKIP.search(t):
            continue
        if "zł" in t.lower() and len(t) < 24:
            continue
        return t[:180]
    for ln in lines:
        t = ln.strip()
        if t and not _TITLE_SKIP.search(t):
            return t[:180]
    return lines[0][:180] if lines else "Bez tytułu"


def _in_poznan(listing: Listing) -> bool:
    blob = (
        f"{listing.title}\n{listing.location}\n{listing.district}\n"
        f"{listing.raw_text[:600]}"
    ).lower()
    if "poznań" in blob or "poznan" in blob:
        return True
    return any(d.lower() in blob for d in DISTRICTS)


_NOT_DISTRICT = re.compile(
    r"wielkopolsk|małopolsk|malopolsk|mazowieck|śląsk|slask|dolnośl|podkarpack",
    re.IGNORECASE,
)


def detect_district(*parts: str) -> str:
    """Район Познані з назви/адреси. Воєводство (wielkopolskie) — не район."""
    blob = " ".join(p for p in parts if p)
    low = blob.lower()
    for name in sorted(DISTRICTS, key=len, reverse=True):
        if name.lower() in low:
            return name
    return ""


def _matches_districts(listing: Listing, districts: list[str]) -> bool:
    if not districts:
        return True
    found = detect_district(
        listing.title,
        listing.location,
        listing.district,
        listing.raw_text[:1200],
    )
    if not found:
        return False
    return found in districts


def _matches_rooms(listing: Listing, rooms: list[int]) -> bool:
    if not rooms:
        return True
    if listing.rooms_hint is None:
        return True
    return listing.rooms_hint in rooms


def _matches_budget(listing: Listing, filters: dict[str, Any]) -> bool:
    min_price = int(filters.get("min_price") or 0)
    max_price = int(filters.get("max_price") or 99999)
    info = listing.price_info or analyze_price(listing.price, listing.raw_text)
    listing.price_info = info
    return fits_budget(info, min_price, max_price)


_NON_FLAT = (
    "garaż",
    "garaz",
    "miejsce parkingowe",
    "parkingowe",
    "boks",
    "magazyn",
    "lokal użytkowy",
    "lokal uzytkowy",
    "biuro",
    "pokój do wynajęcia",
    "pokoj do wynajecia",
)


def _is_apartment(listing: Listing) -> bool:
    blob = f"{listing.title}\n{listing.raw_text}".lower()
    if any(x in blob for x in _NON_FLAT):
        # виняток: якщо явно mieszkanie
        if "mieszkanie" in blob or "kawalerka" in blob:
            return True
        return False
    return True


def listing_matches_user(listing: Listing, filters: dict[str, Any]) -> bool:
    f = {**DEFAULT_FILTERS, **filters}
    if not _in_poznan(listing):
        return False
    if not _is_apartment(listing):
        return False
    if not _matches_districts(listing, f.get("districts") or []):
        return False
    if not _matches_budget(listing, f):
        return False
    if not _matches_rooms(listing, list(f.get("rooms") or [])):
        return False

    pets = f.get("pets", "all")
    if pets in {"yes", "no"} and listing.pets_hint in {"yes", "no"}:
        if listing.pets_hint != pets:
            return False

    owner = f.get("owner_type", "all")
    if owner in {"private", "agency"} and listing.owner_hint in {"private", "agency"}:
        if listing.owner_hint != owner:
            return False

    if f.get("najem_okazjonalny") == "only" and not listing.najem_hint:
        return False
    return True


def _parse_olx_card(card) -> Listing | None:
    try:
        link_el = card.locator(
            "a[href*='/d/oferta'], a[href*='olx.pl'], a[href*='otodom.pl']"
        ).first
        href = link_el.get_attribute("href") if link_el.count() else None
        if not href:
            return None
        url = href if href.startswith("http") else urljoin(OLX_BASE, href)
        url = url.split("#")[0]

        match = (
            re.search(r"-ID([A-Za-z0-9]+)\.html", url)
            or re.search(r"-ID([A-Za-z0-9]+)(?:\?|$)", url)
            or re.search(r"/d/oferta/[^/]+-([A-Za-z0-9]+)", url)
            or re.search(r"/oferta/[^/]+-ID([A-Za-z0-9]+)", url)
        )
        listing_id = match.group(1) if match else url
        source = "otodom" if "otodom.pl" in url else "olx"

        title_el = card.locator("h4").first
        title = title_el.inner_text().strip() if title_el.count() else ""
        text = card.inner_text().strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not title:
            title = lines[0] if lines else "Bez tytułu"

        price = ""
        price_el = card.locator('[data-testid="ad-price"]').first
        if price_el.count():
            price = price_el.inner_text().strip()
        if not price:
            for ln in lines:
                if "zł" in ln.lower() or "pln" in ln.lower():
                    price = ln
                    break

        location = next(
            (ln for ln in lines if "Poznań" in ln or "Poznan" in ln), ""
        )
        area = next((ln for ln in lines if "m²" in ln or "m2" in ln.lower()), "")

        photo_url = _best_image_url(card)

        blob = f"{title}\n{text}"
        listing = Listing(
            id=f"{source}:{listing_id}",
            title=title[:180],
            price=price or "cena nieznana",
            location=location,
            area=area,
            url=url,
            source=source,
            photo_url=photo_url,
            raw_text=blob,
        )
        _stamp_posted_from_card(listing, blob)
        return _enrich(listing)
    except Exception as exc:
        logger.debug("OLX card parse error: %s", exc)
        return None


def _parse_otodom_card(card) -> Listing | None:
    try:
        link = card.locator("a[href*='/pl/oferta/']").first
        href = link.get_attribute("href") if link.count() else None
        if not href:
            return None
        url = href if href.startswith("http") else urljoin(OTODOM_BASE, href)
        url = url.split("?")[0]

        match = re.search(r"-ID([A-Za-z0-9]+)", url)
        listing_id = match.group(1) if match else url

        text = card.inner_text().strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        title = _pick_listing_title(lines)

        price = next(
            (ln for ln in lines if "zł" in ln.lower() or "/mc" in ln.lower()),
            "cena nieznana",
        )
        location = next(
            (ln for ln in lines if "Poznań" in ln or "ul." in ln.lower()), ""
        )
        area = next((ln for ln in lines if "m²" in ln or "m2" in ln.lower()), "")

        photo_url = _best_image_url(card)

        listing = Listing(
            id=f"otodom:{listing_id}",
            title=title[:180],
            price=price,
            location=location,
            area=area,
            url=url,
            source="otodom",
            photo_url=photo_url,
            raw_text=f"{title}\n{text}",
        )
        _stamp_posted_from_card(listing, f"{title}\n{text}")
        return _enrich(listing)
    except Exception as exc:
        logger.debug("Otodom card parse error: %s", exc)
        return None


def _parse_facebook_card(card) -> Listing | None:
    try:
        link = card.locator("a[href*='/marketplace/item/']").first
        href = link.get_attribute("href") if link.count() else None
        if not href:
            return None
        url = href if href.startswith("http") else f"https://www.facebook.com{href}"
        url = url.split("?")[0]
        match = re.search(r"/marketplace/item/(\d+)", url)
        listing_id = match.group(1) if match else url

        text = card.inner_text().strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None

        price = next((ln for ln in lines if "zł" in ln.lower() or "PLN" in ln), lines[0])
        title = next((ln for ln in lines if "zł" not in ln.lower() and len(ln) > 5), lines[0])
        location = next(
            (ln for ln in lines if "Poznań" in ln or "Poznan" in ln or "Poland" in ln),
            "Poznań",
        )

        photo_url = None
        img = card.locator("img").first
        if img.count():
            photo_url = img.get_attribute("src")

        return _enrich(
            Listing(
                id=f"facebook:{listing_id}",
                title=title[:180],
                price=price,
                location=location,
                area="",
                url=url,
                source="facebook",
                photo_url=photo_url,
                raw_text=text,
            )
        )
    except Exception as exc:
        logger.debug("Facebook card parse error: %s", exc)
        return None


def _filter_list(items: list[Listing], filters: dict[str, Any], limit: int) -> list[Listing]:
    out: list[Listing] = []
    for item in items:
        if listing_matches_user(item, filters):
            out.append(item)
            if len(out) >= limit:
                break
    return out


def _prefilter_watch_feed(items: list[Listing]) -> list[Listing]:
    """Усі картки зі стрічки LATEST — фільтр користувача пізніше (watch)."""
    out: list[Listing] = []
    for item in items:
        if _in_poznan(item) and _is_apartment(item):
            out.append(item)
    return out


def fetch_olx_listings(
    filters: dict[str, Any],
    limit: int,
    page,
    page_num: int = 1,
    *,
    for_watch: bool = False,
) -> list[Listing]:
    url = _build_olx_url(filters, page_num)
    logger.info("OLX URL: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _accept_cookies(page)
        page.wait_for_selector('[data-cy="l-card"]', timeout=15000)
    except Exception:
        logger.warning("OLX fetch failed")
        return []

    cards = page.locator('[data-cy="l-card"]')
    n_cards = cards.count()
    scan = (
        min(n_cards, WATCH_MAX_CARD_SCAN)
        if for_watch
        else min(n_cards, max(limit * 3, 24))
    )
    raw: list[Listing] = []
    for i in range(scan):
        item = _parse_olx_card(cards.nth(i))
        if item:
            raw.append(item)
    if for_watch:
        return _prefilter_watch_feed(raw)
    return _filter_list(raw, filters, limit)


def fetch_otodom_listings(
    filters: dict[str, Any],
    limit: int,
    page,
    page_num: int = 1,
    *,
    for_watch: bool = False,
) -> list[Listing]:
    url = _build_otodom_url(filters, page_num)
    logger.info("Otodom URL: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        _accept_cookies(page)
        page.wait_for_selector(
            'a[href*="/pl/oferta/"], [data-cy="listing-item"]', timeout=8000
        )
    except Exception:
        logger.warning("Otodom fetch failed")
        return []

    articles = page.locator("article")
    if articles.count() > 0:
        cards = articles
    else:
        cards = page.locator('[data-cy="listing-item"], li[data-cy="listing-item"]')
    count = cards.count()
    if count == 0:
        logger.warning("Otodom cards not found, title=%s", page.title())
        return []

    scan = (
        min(count, WATCH_MAX_CARD_SCAN)
        if for_watch
        else min(count, max(limit * 3, 36))
    )
    raw: list[Listing] = []
    seen_ids: set[str] = set()
    for i in range(scan):
        item = _parse_otodom_card(cards.nth(i))
        if not item or item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        raw.append(item)
    if for_watch:
        return _prefilter_watch_feed(raw)
    return _filter_list(raw, filters, limit)


def fetch_facebook_listings(filters: dict[str, Any], limit: int, page) -> list[Listing]:
    logger.info("Facebook Marketplace URL: %s", FB_MARKET_URL)
    try:
        page.goto(FB_MARKET_URL, wait_until="domcontentloaded", timeout=45000)
        _accept_cookies(page)
        page.wait_for_timeout(3500)
    except Exception:
        logger.warning("Facebook fetch failed")
        return []

    cards = page.locator('a[href*="/marketplace/item/"]')
    count = cards.count()
    if count == 0:
        logger.warning("Facebook cards not found (можливий login wall)")
        return []

    raw: list[Listing] = []
    seen: set[str] = set()
    for i in range(min(count, max(limit * 4, 40))):
        # картка = батьківський контейнер лінка
        link = cards.nth(i)
        card = link.locator("xpath=ancestor::div[contains(@class,'')][3]")
        if not card.count():
            card = link
        item = _parse_facebook_card(card if card.count() else link)
        if not item or item.id in seen:
            # мінімальний fallback прямо з лінка
            if item and item.id not in seen:
                pass
            else:
                try:
                    href = link.get_attribute("href") or ""
                    url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                    url = url.split("?")[0]
                    m = re.search(r"/marketplace/item/(\d+)", url)
                    if not m:
                        continue
                    lid = m.group(1)
                    if f"facebook:{lid}" in seen:
                        continue
                    text = link.inner_text().strip()
                    item = _enrich(
                        Listing(
                            id=f"facebook:{lid}",
                            title=(text.split("\n")[0] if text else f"FB {lid}")[:180],
                            price=next(
                                (ln for ln in text.splitlines() if "zł" in ln.lower()),
                                "cena nieznana",
                            ),
                            location="Poznań",
                            area="",
                            url=url,
                            source="facebook",
                            raw_text=text,
                        )
                    )
                except Exception:
                    continue
        if not item or item.id in seen:
            continue
        seen.add(item.id)
        raw.append(item)
    return _filter_list(raw, filters, limit)


def _district_name(location: str) -> str:
    found = detect_district(location)
    if found:
        return found
    if not location or _NOT_DISTRICT.search(location):
        return ""
    match = re.search(r"Pozna[nń]\s*,\s*([^-\n,]+)", location, re.IGNORECASE)
    if match:
        raw = match.group(1).strip()
        if _NOT_DISTRICT.search(raw):
            return ""
        return raw
    return ""


def _pl_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text.split(",")[0].split(".")[0])
    if not digits:
        return None
    value = int(digits)
    if value > 100000:
        return None
    return value


def _upgrade_photo(url: str) -> str:
    return re.sub(r";s=\d+x\d+", ";s=1200x900", url)


def _is_real_photo_url(src: str) -> bool:
    low = src.lower()
    if not src.startswith("http"):
        return False
    if any(bad in low for bad in (".svg", "logo", "/icon", "sprite", "placeholder", "avatar")):
        return False
    return "olxcdn.com" in low or "apollo.olxcdn" in low


_UK_MONTHS = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}


def format_posted_dt(
    moment: datetime,
    display_tz: str | None = None,
    *,
    show_time: bool = True,
) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    zone = ZoneInfo(display_tz) if display_tz else WARSAW_TZ
    local = moment.astimezone(zone)
    line = f"Додано {local.day} {_UK_MONTHS[local.month]} {local.year}"
    if show_time:
        line += f" о {local:%H:%M}"
    return line


def parse_posted_from_body(text: str, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """Те, що видно на OLX/Otodom: «Dodane 09 listopada 2026» або з годиною."""
    if not text:
        return None, False
    now = _as_warsaw(now or datetime.now(WARSAW_TZ))
    match = re.search(
        r"Dodane\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)"
        r"(?:\s+(\d{4}))?(?:\s+o\s+(\d{1,2}:\d{2}))?",
        text,
        re.IGNORECASE,
    )
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            year = int(match.group(3)) if match.group(3) else now.year
            clock = match.group(4)
            try:
                if clock:
                    moment = _as_warsaw(
                        datetime(
                            year,
                            month,
                            int(match.group(1)),
                            int(clock[:2]),
                            int(clock[3:]),
                        )
                    )
                    return moment, True
                moment = _as_warsaw(datetime(year, month, int(match.group(1)), 12, 0))
                return moment, False
            except ValueError:
                pass
    snippet = ""
    head = re.search(r"Dodane\s+([^\n]{3,80})", text, re.IGNORECASE)
    if head:
        snippet = head.group(1)
    moment = parse_pl_datetime(snippet or text, now=now)
    if moment is None:
        return None, False
    has_time = bool(re.search(r"\d{1,2}:\d{2}", snippet or text))
    return moment, has_time


def parse_posted_from_html(html: str) -> datetime | None:
    if not html:
        return None
    for key in ("createdAt", "datePublished", "pushupPublishDate"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', html)
        if not match:
            continue
        raw = match.group(1)
        try:
            if raw.endswith("Z"):
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return datetime.fromisoformat(raw)
        except ValueError:
            continue
    return None


def describe_posted(text: str, *, now: datetime | None = None) -> str:
    """Людська дата публікації з тексту картки або сторінки."""
    if not text:
        return ""
    now = now or datetime.now(WARSAW_TZ)
    low = text.lower()
    time_m = re.search(r"(?:\bo\s+|,\s*)(\d{1,2}:\d{2})", text, re.IGNORECASE)
    clock = time_m.group(1) if time_m else ""
    if any(word in low for word in ("dzisiaj", "dzis", "сьогодні")):
        return format_posted_dt(now.replace(hour=int(clock[:2]) if clock else 12, minute=int(clock[3:]) if clock else 0))
    if "wczoraj" in low or "вчора" in low:
        day = now - timedelta(days=1)
        if clock:
            return format_posted_dt(
                day.replace(hour=int(clock[:2]), minute=int(clock[3:]), second=0, microsecond=0)
            )
        return format_posted_dt(day)
    moment = parse_pl_datetime(text, now=now)
    if moment is None:
        return ""
    if clock:
        try:
            moment = moment.replace(hour=int(clock[:2]), minute=int(clock[3:]), second=0, microsecond=0)
        except ValueError:
            pass
    if moment.year < 2000:
        moment = moment.replace(year=now.year)
    return format_posted_dt(moment)


_STREET_PREFIX = {
    "ul": "ul.",
    "ulica": "ul.",
    "al": "al.",
    "aleja": "al.",
    "os": "os.",
    "osiedle": "os.",
    "pl": "pl.",
    "plac": "pl.",
}


def _normalize_street(raw: str, kind: str = "ul") -> str:
    name = re.sub(r"\s+", " ", raw).strip(" ,.-")
    name = re.sub(r"\s+(?:\d+[a-zA-Z]?)(?:[/-]\d+)?(?:\s*.*)?$", "", name)
    if not name or len(name) < 3:
        return ""
    prefix = _STREET_PREFIX.get(kind.lower(), "ul.")
    if name.lower().startswith(("ul.", "ulica", "al.", "aleja", "os.", "pl.")):
        return name[:80]
    return f"{prefix} {name}"[:80]


def _extract_street(text: str) -> str:
    if not text:
        return ""
    patterns = [
        (
            r"(?:\b)(ul\.|ulica)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9][\wąćęłńóśźż.\- ]{1,50})",
            "ul",
        ),
        (
            r"(?:\b)(al\.|aleja)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9][\wąćęłńóśźż.\- ]{1,50})",
            "al",
        ),
        (
            r"(?:\b)(os\.|osiedle|pl\.|plac)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9][\wąćęłńóśźż.\- ]{1,50})",
            "os",
        ),
        (
            r"(?:na|przy|od|w)\s+(?:ulicy|ul\.)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9][\wąćęłńóśźż.\- ]{1,50})",
            "ul",
        ),
        (
            r"Poznań[^,\n]{0,40},\s*(?:ul\.|ulica)\s+"
            r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż0-9][\wąćęłńóśźż.\- ]{1,50})",
            "ul",
        ),
    ]
    for pat, kind in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            raw = match.group(match.lastindex or 1)
            street = _normalize_street(raw, kind)
            if street:
                return street
    return ""


_MONTHS = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "wrzesnia": 9,
    "października": 10,
    "pazdziernika": 10,
    "listopada": 11,
    "grudnia": 12,
}


def _plausible_posted(
    moment: datetime, now: datetime | None = None
) -> datetime | None:
    """Корекція року (без року OLX часто ставить поточний) і відсікання неможливих дат."""
    now = _as_warsaw(now or datetime.now(WARSAW_TZ))
    moment = _as_warsaw(moment)
    if moment > now + timedelta(hours=6):
        try:
            moment = moment.replace(year=moment.year - 1)
        except ValueError:
            return None
    if moment > now + timedelta(hours=6):
        return None
    if moment < now - timedelta(days=450):
        return None
    return moment


def parse_pl_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """Дата з польського тексту картки або сторінки оголошення."""
    if not text:
        return None
    now = _as_warsaw(now or datetime.now(WARSAW_TZ))
    low = text.lower()
    if "dzisiaj" in low or "dzis" in low or "сьогодні" in low:
        return now
    if "wczoraj" in low or "вчора" in low:
        return now - timedelta(days=1)
    match = re.search(
        r"(\d{1,2})\s+([a-ząćęłńóśźż]+)(?:\s+(\d{4}))?",
        low,
    )
    if not match:
        return None
    month = _MONTHS.get(match.group(2))
    if not month:
        return None
    year = int(match.group(3)) if match.group(3) else now.year
    try:
        moment = _as_warsaw(datetime(year, month, int(match.group(1))))
    except ValueError:
        return None
    if not match.group(3):
        return _plausible_posted(moment, now)
    return moment


def listing_posted_at(listing: Listing) -> datetime | None:
    iso = listing.extras.get("posted_iso")
    if iso:
        try:
            moment = datetime.fromisoformat(iso)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ZoneInfo("UTC"))
            moment = moment.astimezone(WARSAW_TZ)
            return _plausible_posted(moment)
        except ValueError:
            pass
    if listing.posted_label:
        moment = parse_pl_datetime(listing.posted_label)
        if moment:
            return _plausible_posted(moment)
    dodane = re.search(r"Dodane\s+([^\n]{3,80})", listing.raw_text, re.IGNORECASE)
    if dodane:
        moment = parse_pl_datetime(dodane.group(1))
        if moment:
            return _plausible_posted(moment)
    return None


def listing_posted_within(listing: Listing, days: int) -> bool:
    """Для меню й алертів: лише з відомою датою в межах `days`."""
    moment = listing_posted_at(listing)
    if moment is None:
        return False
    cutoff = datetime.now(WARSAW_TZ) - timedelta(days=days)
    return moment >= cutoff


def is_within_days(listing: Listing, days: int = 7, *, strict: bool = False) -> bool:
    if strict or days <= 14:
        return listing_posted_within(listing, days)
    moment = listing_posted_at(listing)
    if moment is None:
        return False
    cutoff = datetime.now(WARSAW_TZ) - timedelta(days=days)
    return moment >= cutoff


def is_fresh_for_watch(listing: Listing, days: int) -> bool:
    """Авто-сповіщення: лише з датою в межах вікна (після enrich)."""
    return listing_posted_within(listing, days)


def _apply_detail_text(listing: Listing, blob: str) -> None:
    listing.raw_text = (listing.raw_text + "\n" + blob)[:20000]
    czynsz_m = re.search(
        r"Czynsz(?:\s*\([^)]+\))?\s*:?\s*([\d \u00a0]+)",
        blob,
        re.IGNORECASE,
    )
    if czynsz_m:
        listing.czynsz_zl = _pl_int(czynsz_m.group(1)) or 0

    area_m = re.search(
        r"Powierzchnia\s*:?\s*([\d]+(?:[.,]\d+)?)",
        blob,
        re.IGNORECASE,
    )
    if area_m:
        listing.area_m2 = float(area_m.group(1).replace(",", "."))
        listing.area = f"{area_m.group(1)} m²"

    rooms_m = re.search(
        r"(?:Liczba pokoi|pokoje)\s*:?\s*(\d+)",
        blob,
        re.IGNORECASE,
    )
    if rooms_m:
        listing.rooms_hint = int(rooms_m.group(1))

    furn_m = re.search(r"Umeblowane\s*:?\s*(Tak|Nie)", blob, re.IGNORECASE)
    if furn_m:
        listing.furnished = furn_m.group(1).lower()

    if listing.extras.get("posted_iso"):
        try:
            listing.posted_label = format_posted_dt(
                datetime.fromisoformat(listing.extras["posted_iso"]),
                show_time=bool(listing.extras.get("posted_has_time")),
            )
        except ValueError:
            pass
    if not listing.posted_label:
        date_m = re.search(r"Dodane\s+([^\n]{3,60})", blob, re.IGNORECASE)
        if date_m:
            listing.posted_label = describe_posted(date_m.group(1)) or describe_posted(blob)
        if not listing.posted_label:
            listing.posted_label = describe_posted(listing.location) or describe_posted(blob)
    combined = "\n".join(
        part
        for part in (listing.title, listing.location, blob, listing.raw_text)
        if part
    )
    if not listing.street:
        listing.street = _extract_street(combined)

    if re.search(r"prywatn|osoba prywatna", blob, re.IGNORECASE):
        listing.owner_hint = "private"
    elif re.search(r"biuro nieruchomości|agencja|deweloper", blob, re.IGNORECASE):
        listing.owner_hint = "agency"

    rent = _pl_int(listing.price) if listing.price else None
    if rent is None:
        rent_m = re.search(r"(\d{1,2}(?:[ \u00a0]\d{3})|\d{3,5})\s*zł", blob)
        if rent_m:
            rent = _pl_int(rent_m.group(1))
    listing.rent_zl = rent
    listing.district = (
        detect_district(listing.title, listing.location, blob[:1200])
        or listing.district
    )
    if listing.district and _NOT_DISTRICT.search(listing.district):
        listing.district = detect_district(listing.title, blob[:1200])
    czynsz = listing.czynsz_zl if listing.czynsz_zl is not None else 0
    if rent is not None:
        listing.price_info = PriceBreakdown(
            base=rent,
            czynsz=czynsz,
            media=0,
            total=rent + czynsz,
            inclusive=False,
            estimated=listing.czynsz_zl is None,
            note="detail",
        )
    listing.details_loaded = True


def _photos_from_html(html: str) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"https://[^\"'\s]+apollo\.olxcdn\.com[^\"'\s]+/image[^\"'\s]*",
        html,
    ):
        src = match.group(0).replace("\\u002F", "/")
        if not _is_real_photo_url(src):
            continue
        key = src.split(";")[0]
        if key in seen:
            continue
        seen.add(key)
        photos.append(_upgrade_photo(src))
        if len(photos) >= 4:
            break
    if not photos:
        og = re.search(r'property="og:image" content="([^"]+)"', html)
        if og and _is_real_photo_url(og.group(1)):
            photos.append(_upgrade_photo(og.group(1)))
    return photos


def _read_photos(page) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    imgs = page.locator("img")
    for i in range(min(imgs.count(), 40)):
        src = imgs.nth(i).get_attribute("src") or ""
        if not _is_real_photo_url(src):
            continue
        key = src.split(";")[0]
        if key in seen:
            continue
        seen.add(key)
        photos.append(_upgrade_photo(src))
        if len(photos) >= 4:
            break
    return photos


def _open_browser():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        locale="pl-PL",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    return playwright, browser, page


def enrich_on_page(page, listing: Listing) -> Listing:
    if listing.details_loaded or "facebook.com" in (listing.url or ""):
        listing.details_loaded = True
        return listing
    try:
        page.goto(listing.url, wait_until="domcontentloaded", timeout=18000)
        _accept_cookies(page)
        html = page.content()
        text = page.inner_text("body")
        visible_dt, has_time = parse_posted_from_body(text)
        posted_dt = visible_dt or parse_posted_from_html(html)
        if has_time or visible_dt:
            listing.extras["posted_has_time"] = has_time
        elif parse_posted_from_html(html):
            listing.extras["posted_has_time"] = True
        else:
            listing.extras["posted_has_time"] = False
        if posted_dt:
            if visible_dt:
                posted_dt = visible_dt
            posted_dt = _plausible_posted(_as_warsaw(posted_dt))
            if posted_dt:
                listing.extras["posted_iso"] = posted_dt.isoformat()
        photos = _read_photos(page) or _photos_from_html(html)
        if photos:
            listing.photos = photos
            listing.photo_url = photos[0]
        elif listing.photo_url and _is_real_photo_url(listing.photo_url):
            listing.photos = [_upgrade_photo(listing.photo_url)]
        h1 = page.locator("h1").first
        if h1.count():
            t = h1.inner_text().strip()
            if t and len(t) > 8 and not _TITLE_SKIP.search(t):
                listing.title = t[:180]
        _apply_detail_text(listing, text)
        listing.details_loaded = True
    except Exception:
        logger.warning("detail skip %s", listing.id, exc_info=True)
    return listing


def enrich_many(listings: list[Listing]) -> list[Listing]:
    """Один браузер на всю пачку — без окремого запуску на кожне оголошення."""
    pending = [x for x in listings if not x.details_loaded and "facebook.com" not in (x.url or "")]
    if not pending:
        return listings
    playwright, browser, page = _open_browser()
    try:
        for item in pending:
            enrich_on_page(page, item)
    finally:
        browser.close()
        playwright.stop()
    return listings


def load_listing_details(listing: Listing) -> Listing:
    enrich_many([listing])
    return listing


def _dedupe(items: list[Listing], limit: int | None = None) -> list[Listing]:
    unique: list[Listing] = []
    seen_ids: set[str] = set()
    fingerprints: set[str] = set()
    for item in items:
        if item.id in seen_ids:
            continue
        fp = re.sub(r"\W+", "", f"{item.title}{item.price}".lower())[:60]
        if fp in fingerprints:
            continue
        seen_ids.add(item.id)
        fingerprints.add(fp)
        unique.append(item)
        if limit and len(unique) >= limit:
            break
    return unique


def search_listings(
    filters: dict[str, Any] | None = None,
    limit: int = 15,
) -> list[Listing]:
    """Свіжі оголошення тільки з OLX і Otodom. Facebook сюди не входить."""
    f = {**DEFAULT_FILTERS, **(filters or {})}
    per_source = max(limit, 12)
    collected: list[Listing] = []

    playwright, browser, page = _open_browser()
    try:
        for fetcher in (fetch_olx_listings, fetch_otodom_listings):
            try:
                collected.extend(fetcher(f, per_source, page))
            except Exception:
                logger.exception("source failed: %s", fetcher.__name__)
    finally:
        browser.close()
        playwright.stop()

    unique = _dedupe(collected, limit)
    logger.info("OLX+Otodom unique: %s", len(unique))
    return unique


def _in_last_days_window(listing: Listing, days: int) -> bool:
    return listing_posted_within(listing, days)


def _collect_last_days_raw(
    page,
    f: dict[str, Any],
    days: int,
    *,
    max_pages: int = 3,
) -> list[Listing]:
    collected: list[Listing] = []
    for fetcher in (fetch_olx_listings, fetch_otodom_listings):
        empty_hits = 0
        for page_num in range(1, max_pages + 1):
            try:
                batch = fetcher(f, 36, page, page_num=page_num)
            except Exception:
                logger.exception("page failed %s p%s", fetcher.__name__, page_num)
                break
            if not batch:
                empty_hits += 1
                if empty_hits >= 2:
                    break
                continue
            empty_hits = 0
            collected.extend(batch)
            hits = sum(
                1
                for item in batch
                if listing_matches_user(item, f) and is_within_days(item, days)
            )
            if hits == 0 and page_num >= 2:
                break
    return [
        item
        for item in _dedupe(collected)
        if listing_matches_user(item, f) and is_within_days(item, days)
    ]


def _finalize_week_batch(
    matched: list[Listing],
    f: dict[str, Any],
    days: int,
    *,
    result_limit: int = 20,
) -> list[Listing]:
    fresh = [
        item
        for item in matched
        if listing_matches_user(item, f) and _in_last_days_window(item, days)
    ]
    fresh.sort(
        key=lambda x: listing_posted_at(x) or datetime.min.replace(tzinfo=WARSAW_TZ),
        reverse=True,
    )
    return fresh[:result_limit]


def collect_last_days(
    filters: dict[str, Any] | None = None,
    days: int = 7,
    max_pages: int = 3,
) -> list[Listing]:
    """Оголошення за останні `days` днів (обмежений скан для хмари)."""
    f = {**DEFAULT_FILTERS, **(filters or {})}
    playwright, browser, page = _open_browser()
    try:
        matched = _collect_last_days_raw(page, f, days, max_pages=max_pages)[:25]
        for item in matched[:12]:
            enrich_on_page(page, item)
    finally:
        browser.close()
        playwright.stop()

    fresh = _finalize_week_batch(matched, f, days)
    logger.info("Last %s days: %s listings", days, len(fresh))
    return fresh


def listing_record(item: Listing) -> dict[str, Any]:
    import json

    payload = {
        "photos": item.photos,
        "rent_zl": item.rent_zl,
        "czynsz_zl": item.czynsz_zl,
        "area_m2": item.area_m2,
        "rooms_hint": item.rooms_hint,
        "furnished": item.furnished,
        "owner_hint": item.owner_hint,
        "posted_label": item.posted_label,
        "district": item.district,
        "photo_url": item.photo_url,
    }
    return {
        "id": item.id,
        "title": item.title,
        "price": item.price,
        "location": item.location,
        "area": item.area,
        "url": item.url,
        "source": item.source,
        "payload": json.dumps(payload, ensure_ascii=False),
        "posted_at": item.posted_label,
    }


def listing_snapshot(item: Listing) -> dict[str, Any]:
    import json

    rec = listing_record(item)
    try:
        payload = json.loads(rec["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        **rec,
        "payload": payload,
        "street": item.street,
        "district": item.district,
        "posted_label": item.posted_label,
        "extras": dict(item.extras),
        "details_loaded": item.details_loaded,
    }


def listing_from_snapshot(snap: dict[str, Any]) -> Listing:
    payload = snap.get("payload") or {}
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return Listing(
        id=snap["id"],
        title=snap.get("title") or "",
        price=snap.get("price") or "",
        location=snap.get("location") or "",
        area=snap.get("area") or "",
        url=snap["url"],
        source=snap.get("source") or "olx",
        photo_url=payload.get("photo_url"),
        photos=list(payload.get("photos") or []),
        rent_zl=payload.get("rent_zl"),
        czynsz_zl=payload.get("czynsz_zl"),
        area_m2=payload.get("area_m2"),
        rooms_hint=payload.get("rooms_hint"),
        furnished=payload.get("furnished"),
        owner_hint=payload.get("owner_hint") or "unknown",
        posted_label=snap.get("posted_label") or payload.get("posted_label") or "",
        district=snap.get("district") or payload.get("district") or "",
        street=snap.get("street") or "",
        details_loaded=bool(snap.get("details_loaded", True)),
        extras=dict(snap.get("extras") or {}),
    )
