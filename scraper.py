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
    r"^(dodane|dzisiaj|dzis|wczoraj|odświe|odswie|today|yesterday|"
    r"\d+\s+(?:minut|godzin)|"
    r"\d+\s*/\s*\d+|"
    r"promowane|liczba pokoi|piętro|pietro|cena za metr|\+?\s*czynsz)",
    re.IGNORECASE,
)
_LOC_DATE_TAIL = re.compile(
    r"\s*[-–]\s*(?:odświeżono\s+dnia\s+)?\d{1,2}\s+[a-ząćęłńóśźż]+(?:\s+\d{4})?\s*$",
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

    def format_caption(
        self,
        *,
        embed_links: bool = False,
        display_tz: str | None = None,
        date_lang: str = "uk",
    ) -> str:
        """Картка 1:1 як у Warsaw Rent (HTML)."""
        import html as html_lib

        title = html_lib.escape(self.title)
        district = detect_district(
            self.title, self.location, self.district, self.raw_text[:800]
        )
        maps = self.maps_url()
        rent, czynsz = listing_rent_czynsz(self)
        if rent is None:
            price_line = "💰 ціна не вказана"
            total = None
        else:
            total = rent + czynsz
            # Як у Warsaw Rent: 3 100 zł + czynsz 850 = 3 950 zł · ≈ 84 zł/m²
            price_line = (
                f"💰 {self._money(rent)} zł + czynsz {self._money(czynsz)} "
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

        show_time = bool(self.extras.get("posted_has_time"))
        if self.extras.get("posted_iso"):
            try:
                posted = format_posted_dt(
                    datetime.fromisoformat(self.extras["posted_iso"]),
                    display_tz,
                    show_time=show_time,
                    lang=date_lang,
                )
            except ValueError:
                posted = self.posted_label or describe_posted(self.location)
        else:
            posted = (
                self.posted_label
                or describe_posted(self.raw_text)
                or describe_posted(self.location)
            )

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
    """Дата з картки: «Poznań, X - 09 listopada 2026», Odświeżono, Dodane wczoraj."""
    if listing.extras.get("posted_iso"):
        return
    moment, has_time = parse_card_posted(text)
    if not moment:
        return
    listing.extras["posted_iso"] = moment.isoformat()
    listing.extras["posted_has_time"] = has_time
    listing.posted_label = format_posted_dt(moment, show_time=has_time, lang="pl")


def _clean_location(location: str) -> str:
    if not location:
        return ""
    return _LOC_DATE_TAIL.sub("", location).strip(" -,")


def parse_czynsz_zl(text: str) -> int | None:
    """Чинш адміністраційний, не «czynsz najmu» (це оренда) і не слово з заголовка."""
    if not text:
        return None
    patterns = (
        r"\+\s*czynsz\s*:?\s*([\d \u00a0]+)",
        r"czynsz\s*\(\s*dodatkowo\s*\)\s*:?\s*([\d \u00a0]+)",
        r"czynsz\s*administracyjny\s*:?\s*([\d \u00a0]+)",
        r"czynsz\s*:\s*([\d \u00a0]+)",
    )
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            left = text[max(0, match.start() - 18) : match.start()].lower()
            if "najmu" in left:
                continue
            value = _pl_int(match.group(1))
            if value is None:
                continue
            if 80 <= value <= 3500:
                return value
    return None


def _sane_czynsz(rent: int | None, czynsz: int | None) -> int:
    if not czynsz or czynsz <= 0:
        return 0
    if rent and czynsz >= int(rent) * 0.85:
        # «Czynsz 1900!» у заголовку = оренда, не додавати вдруге
        return 0
    return int(czynsz)


def _fill_card_facts(listing: Listing, text: str) -> None:
    """Чинш, площа, кімнати, ціна — з тексту картки, без відкриття оголошення."""
    blob = text or listing.raw_text or ""
    if listing.czynsz_zl is None:
        parsed = parse_czynsz_zl(blob)
        if parsed is not None:
            listing.czynsz_zl = parsed
    area_m = re.search(r"([\d]+(?:[.,]\d+)?)\s*m(?:²|2)\b", blob, re.IGNORECASE)
    if area_m and listing.area_m2 is None:
        listing.area_m2 = float(area_m.group(1).replace(",", "."))
        listing.area = f"{area_m.group(1).replace('.', ',')} m²"
    if listing.rooms_hint is None:
        rooms_m = re.search(r"(\d+)\s*pokoj", blob, re.IGNORECASE)
        if rooms_m:
            listing.rooms_hint = min(int(rooms_m.group(1)), 4)
        else:
            listing.rooms_hint = _infer_rooms(blob)
    rent = _pl_int(listing.price) if listing.price else None
    if rent is not None:
        listing.rent_zl = rent
    if listing.czynsz_zl is not None and listing.rent_zl:
        if listing.czynsz_zl >= int(listing.rent_zl) * 0.85:
            listing.czynsz_zl = None
    low = blob.lower()
    if "oferta prywatna" in low or "osoba prywatna" in low:
        listing.owner_hint = "private"


def _enrich(listing: Listing) -> Listing:
    listing.location = _clean_location(listing.location)
    listing.district = (
        detect_district(listing.title, listing.location, listing.raw_text[:1200])
        or listing.district
    )
    if listing.district and _NOT_DISTRICT.search(listing.district):
        listing.district = detect_district(listing.title, listing.raw_text[:1200])
    _fill_card_facts(listing, listing.raw_text)
    listing.price_info = analyze_price(listing.price, listing.raw_text)
    if listing.owner_hint == "unknown":
        listing.owner_hint = _infer_owner(listing.raw_text)
    listing.pets_hint = _infer_pets(listing.raw_text)
    listing.najem_hint = bool(NAJEM_RE.search(listing.raw_text))
    if listing.rooms_hint is None:
        listing.rooms_hint = _infer_rooms(listing.raw_text)
    if listing.rent_zl is None and listing.price_info and listing.price_info.base:
        listing.rent_zl = listing.price_info.base
    return listing


def _best_image_url(card) -> str | None:
    photos = _photos_from_card(card)
    return photos[0] if photos else None


def _img_urls_from_locator(card) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    imgs = card.locator("img")
    n = min(imgs.count(), 12)
    for i in range(n):
        el = imgs.nth(i)
        srcset = el.get_attribute("srcset") or el.get_attribute("data-srcset") or ""
        src = (
            el.get_attribute("src")
            or el.get_attribute("data-src")
            or el.get_attribute("data-original")
            or ""
        )
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
            continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        url = _normalize_img_url(candidates[0][1])
        if not _is_real_photo_url(url):
            continue
        key = url.split(";")[0].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
        if len(urls) >= 4:
            break
    return urls


def _photos_from_card(card) -> list[str]:
    """Фото прямо з картки списку (після скролу lazy-img вже в DOM)."""
    urls = _img_urls_from_locator(card)
    if urls:
        return urls
    try:
        card.scroll_into_view_if_needed(timeout=800)
    except Exception:
        return []
    return _img_urls_from_locator(card)


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
        return True
    return found in districts


def _matches_rooms(listing: Listing, rooms: list[int]) -> bool:
    if not rooms:
        return True
    if listing.rooms_hint is None:
        return True
    return listing.rooms_hint in rooms


def listing_rent_czynsz(listing: Listing) -> tuple[int | None, int]:
    """Оренда і чинш для картки та бюджету (без вигаданих media)."""
    rent = listing.rent_zl
    if rent is None and listing.price_info and listing.price_info.base:
        rent = listing.price_info.base
    if rent is None:
        from price import parse_amount

        rent = parse_amount(listing.price)
    czynsz = listing.czynsz_zl
    if czynsz is None:
        czynsz = parse_czynsz_zl(listing.raw_text or "")
    if (
        czynsz is None
        and listing.price_info
        and not listing.price_info.estimated
        and listing.price_info.czynsz
    ):
        czynsz = listing.price_info.czynsz
    return rent, _sane_czynsz(rent, czynsz)


def listing_total_zl(listing: Listing) -> int | None:
    rent, czynsz = listing_rent_czynsz(listing)
    if rent is None:
        return None
    return rent + czynsz


def _matches_budget(listing: Listing, filters: dict[str, Any]) -> bool:
    """Фільтр бюджету = оренда + чинш, як на картці Warsaw Rent."""
    min_price = int(filters.get("min_price") or 0)
    max_price = int(filters.get("max_price") or 99999)
    total = listing_total_zl(listing)
    if total is None:
        return True
    if min_price > 0 and total < min_price:
        return False
    if max_price < 90000 and total > max_price:
        return False
    return True


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
    "wynajem pokoju",
    "pokój dla",
    "pokoju dla",
    "pokój na wynajem",
    "pokoj na wynajem",
)


def _is_apartment(listing: Listing) -> bool:
    blob = f"{listing.title}\n{listing.raw_text}".lower()
    if any(x in blob for x in _NON_FLAT):
        # виняток: якщо явно mieszkanie
        if "mieszkanie" in blob or "kawalerka" in blob or "apartament" in blob:
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
            price = price_el.inner_text().strip().splitlines()[0].strip()
        if not price:
            for ln in lines:
                if "zł" in ln.lower() or "pln" in ln.lower():
                    price = ln.splitlines()[0].strip()
                    break

        location = next(
            (ln for ln in lines[1:] if re.search(r"Pozna[nń]\s*,", ln)),
            next(
                (
                    ln
                    for ln in lines[1:]
                    if "Poznań" in ln or "Poznan" in ln
                ),
                "",
            ),
        )
        area = next((ln for ln in lines if "m²" in ln or "m2" in ln.lower()), "")

        photos = _photos_from_card(card)
        blob = f"{title}\n{text}"
        listing = Listing(
            id=f"{source}:{listing_id}",
            title=title[:180],
            price=price or "cena nieznana",
            location=location,
            area=area,
            url=url.split("?")[0],
            source=source,
            photo_url=photos[0] if photos else None,
            photos=photos,
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
            (
                ln.splitlines()[0].strip()
                for ln in lines
                if ("zł" in ln.lower() or "/mc" in ln.lower())
                and "czynsz" not in ln.lower()
            ),
            "cena nieznana",
        )
        location = next(
            (
                ln
                for ln in lines
                if re.search(r"Pozna[nń]", ln)
                and ("ul." in ln.lower() or "," in ln)
                and "zł" not in ln.lower()
            ),
            next(
                (ln for ln in lines if "Poznań" in ln or "Poznan" in ln),
                "",
            ),
        )
        area = next((ln for ln in lines if "m²" in ln or "m2" in ln.lower()), "")

        photos = _photos_from_card(card)
        blob = f"{title}\n{text}"
        listing = Listing(
            id=f"otodom:{listing_id}",
            title=title[:180],
            price=price,
            location=location,
            area=area,
            url=url,
            source="otodom",
            photo_url=photos[0] if photos else None,
            photos=photos,
            raw_text=blob,
        )
        _stamp_posted_from_card(listing, blob)
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
        _scroll_results(page)
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


_OTODOM_PRICE_JS = """() => {
  const el = document.getElementById('__NEXT_DATA__');
  if (!el || !el.textContent) return {};
  let data;
  try { data = JSON.parse(el.textContent); } catch (e) { return {}; }
  const out = {};
  const num = (v) => {
    if (v == null) return null;
    if (typeof v === 'number' && isFinite(v)) return v;
    if (typeof v === 'object' && typeof v.value === 'number') return v.value;
    return null;
  };
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    const rent = num(node.totalPrice);
    const czynsz = num(node.rentPrice);
    if (rent != null || czynsz != null) {
      const rec = { rent, czynsz };
      if (node.id != null) out[String(node.id)] = rec;
      const slug = node.slug || node.url || '';
      if (slug) {
        const idm = String(slug).match(/ID[A-Za-z0-9]+/);
        if (idm) out[idm[0]] = rec;
      }
    }
    for (const v of Object.values(node)) {
      if (v && typeof v === 'object') walk(v);
    }
  };
  walk(data);
  return out;
}"""


def _apply_otodom_json_prices(page, listings: list[Listing]) -> None:
    """Otodom: totalPrice = оренда, rentPrice = чинш (назви полів навпаки)."""
    if not listings:
        return
    try:
        mapping = page.evaluate(_OTODOM_PRICE_JS)
    except Exception:
        return
    if not mapping:
        return
    for item in listings:
        rec = None
        lid = item.id.split(":", 1)[-1]
        rec = mapping.get(lid) or mapping.get(item.id)
        if rec is None:
            url = item.url or ""
            for key, val in mapping.items():
                if key and len(str(key)) >= 6 and str(key) in url:
                    rec = val
                    break
        if not rec:
            continue
        rent = rec.get("rent")
        czynsz = rec.get("czynsz")
        if rent and not item.rent_zl:
            item.rent_zl = int(rent)
            if not item.price or item.price == "cena nieznana":
                item.price = f"{int(rent)} zł"
        if czynsz is not None and item.czynsz_zl is None:
            cz = int(czynsz)
            if 80 <= cz <= 3500:
                item.czynsz_zl = cz
        if item.czynsz_zl is not None and item.rent_zl:
            if item.czynsz_zl >= int(item.rent_zl) * 0.85:
                item.czynsz_zl = None


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
        _scroll_results(page)
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
    _apply_otodom_json_prices(page, raw)
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


def _normalize_img_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    url = url.replace("https://ireland.apollo.olxcdn.com:443/", "https://ireland.apollo.olxcdn.com/")
    url = re.sub(r":443/", "/", url)
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r";s=\d+x\d+", ";s=1000x700", url)
    url = re.sub(r";q=\d+", ";q=80", url)
    return url


def _upgrade_photo(url: str) -> str:
    return _normalize_img_url(url)


def _scroll_results(page) -> None:
    """Підвантажити lazy-фото на сторінці результатів."""
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
    except Exception:
        pass


def _is_real_photo_url(src: str) -> bool:
    if not src or src.startswith("data:"):
        return False
    low = src.lower()
    if src.startswith("//"):
        src = "https:" + src
        low = src.lower()
    if not src.startswith("http"):
        return False
    if any(
        bad in low
        for bad in (
            ".svg",
            "logo",
            "/icon",
            "sprite",
            "placeholder",
            "avatar",
            "blank.gif",
        )
    ):
        return False
    return any(
        host in low
        for host in (
            "olxcdn.com",
            "apollo.olxcdn",
            "img.otodom.pl",
            "images.otodom.pl",
            "otodom.pl/image",
            "ocdn.eu",
        )
    )


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

_PL_MONTHS = {
    1: "stycznia",
    2: "lutego",
    3: "marca",
    4: "kwietnia",
    5: "maja",
    6: "czerwca",
    7: "lipca",
    8: "sierpnia",
    9: "września",
    10: "października",
    11: "listopada",
    12: "grudnia",
}


def format_posted_dt(
    moment: datetime,
    display_tz: str | None = None,
    *,
    show_time: bool = False,
    lang: str = "uk",
) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=WARSAW_TZ)
    zone = WARSAW_TZ if (not display_tz or lang == "pl") else ZoneInfo(display_tz)
    local = moment.astimezone(zone)
    today = datetime.now(zone).date()
    clock = show_time and not (local.hour == 0 and local.minute == 0)
    if lang == "pl":
        if clock and local.date() == today:
            return f"Dodane o {local:%H:%M}"
        line = f"Dodane {local.day} {_PL_MONTHS[local.month]} {local.year}"
        if clock:
            line += f" o {local:%H:%M}"
        return line
    if clock and local.date() == today:
        return f"Додано о {local:%H:%M}"
    line = f"Додано {local.day} {_UK_MONTHS[local.month]} {local.year}"
    if clock:
        line += f" о {local:%H:%M}"
    return line


def parse_posted_from_body(text: str, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """Те, що видно на OLX/Otodom: «Dodane 09 listopada 2026» або з годиною."""
    return parse_card_posted(text, now=now)


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
    moment, has_time = parse_card_posted(text, now=now)
    if moment is None:
        return ""
    return format_posted_dt(moment, show_time=has_time)


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


_MONTH_ALT = (
    "stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|"
    "września|wrzesnia|października|pazdziernika|listopada|grudnia"
)


def _abs_from_match(
    day_s: str,
    month_s: str,
    year_s: str | None,
    clock: str | None,
    now: datetime,
) -> tuple[datetime | None, bool]:
    month = _MONTHS.get(month_s.lower())
    if not month:
        return None, False
    year = int(year_s) if year_s else now.year
    try:
        if clock:
            moment = _as_warsaw(
                datetime(year, month, int(day_s), int(clock[:2]), int(clock[3:]))
            )
            return _plausible_posted(moment, now), True
        moment = _as_warsaw(datetime(year, month, int(day_s), 12, 0))
        return _plausible_posted(moment, now), False
    except ValueError:
        return None, False


def _rel_word(word: str, clock: str | None, now: datetime) -> tuple[datetime | None, bool]:
    low = word.lower()
    base = now
    if low in {"wczoraj", "вчора"}:
        base = now - timedelta(days=1)
    if clock:
        try:
            moment = base.replace(
                hour=int(clock[:2]),
                minute=int(clock[3:]),
                second=0,
                microsecond=0,
            )
            return moment, True
        except ValueError:
            pass
    moment = base.replace(hour=12, minute=0, second=0, microsecond=0)
    return moment, False


def parse_card_posted(
    text: str, now: datetime | None = None
) -> tuple[datetime | None, bool]:
    """Дата з картки OLX/Otodom. has_time=True лише якщо була година."""
    if not text:
        return None, False
    now = _as_warsaw(now or datetime.now(WARSAW_TZ))

    m = re.search(
        rf"(?:odświeżono|odswiezono)\s+dnia\s+(\d{{1,2}})\s+({_MONTH_ALT})(?:\s+(\d{{4}}))?",
        text,
        re.IGNORECASE,
    )
    if m:
        return _abs_from_match(m.group(1), m.group(2), m.group(3), None, now)

    m = re.search(
        rf"Dodane\s+(\d{{1,2}})\s+({_MONTH_ALT})(?:\s+(\d{{4}}))?(?:\s+o\s+(\d{{1,2}}:\d{{2}}))?",
        text,
        re.IGNORECASE,
    )
    if m:
        return _abs_from_match(m.group(1), m.group(2), m.group(3), m.group(4), now)

    m = re.search(
        r"Dodane\s+(dzisiaj|dzis|wczoraj)(?:\s+o\s+(\d{1,2}:\d{2}))?",
        text,
        re.IGNORECASE,
    )
    if m:
        return _rel_word(m.group(1), m.group(2), now)

    m = re.search(
        rf"[-–]\s+(\d{{1,2}})\s+({_MONTH_ALT})(?:\s+(\d{{4}}))?",
        text,
        re.IGNORECASE,
    )
    if m:
        return _abs_from_match(m.group(1), m.group(2), m.group(3), None, now)

    m = re.search(
        r"(\d+)\s*(minut[aęy]?|min\.?|godzin[aęy]?|godz\.?|dni|dzień|dzien)\s*temu",
        text,
        re.IGNORECASE,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("min"):
            return now - timedelta(minutes=n), True
        if unit.startswith("godz"):
            return now - timedelta(hours=n), True
        day = (now - timedelta(days=n)).replace(hour=12, minute=0, second=0, microsecond=0)
        return day, False

    m = re.search(
        r"(?:^|\n)\s*(dzisiaj|dzis|wczoraj)(?:\s+o\s+(\d{1,2}:\d{2}))?",
        text,
        re.IGNORECASE,
    )
    if m:
        return _rel_word(m.group(1), m.group(2), now)

    return None, False


def parse_pl_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """Дата з польського тексту картки або сторінки оголошення."""
    moment, _has_time = parse_card_posted(text, now=now)
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
    moment, _has_time = parse_card_posted(listing.raw_text or listing.location)
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
    """Миттєве сповіщення: щойно з’явилось у «найновіші» сьогодні.

    Дати немає — верх стрічки LATEST, шлемо.
    Є дата — лише сьогодні / останні `days` доби. Старе — кнопка «За 7 днів».
    """
    moment = listing_posted_at(listing)
    if moment is None:
        return True
    now = datetime.now(WARSAW_TZ)
    if moment.astimezone(WARSAW_TZ).date() == now.date():
        return True
    return listing_posted_within(listing, max(1, days))


def _apply_detail_text(listing: Listing, blob: str) -> None:
    listing.raw_text = (listing.raw_text + "\n" + blob)[:20000]
    parsed_czynsz = parse_czynsz_zl(blob) or parse_czynsz_zl(listing.raw_text)
    if parsed_czynsz is not None:
        listing.czynsz_zl = parsed_czynsz

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
    if listing.czynsz_zl is not None and rent:
        if listing.czynsz_zl >= int(rent) * 0.85:
            listing.czynsz_zl = None
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
        r"https://[^\"'\s]+(?:apollo\.olxcdn\.com|img\.otodom\.pl|images\.otodom\.pl|ocdn\.eu)[^\"'\s]+",
        html,
    ):
        src = match.group(0).replace("\\u002F", "/").rstrip("\\")
        if not _is_real_photo_url(src):
            continue
        key = src.split(";")[0].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        photos.append(_upgrade_photo(src))
        if len(photos) >= 4:
            break
    if not photos:
        og = re.search(
            r'property="og:image"\s+content="([^"]+)"',
            html,
        ) or re.search(r'content="([^"]+)"\s+property="og:image"', html)
        if og:
            src = og.group(1)
            if _is_real_photo_url(src) or src.startswith("http"):
                photos.append(_upgrade_photo(src))
    return photos


def _read_photos(page) -> list[str]:
    photos: list[str] = []
    seen: set[str] = set()
    imgs = page.locator("img")
    for i in range(min(imgs.count(), 40)):
        el = imgs.nth(i)
        src = (
            el.get_attribute("src")
            or el.get_attribute("data-src")
            or el.get_attribute("data-original")
            or ""
        )
        if src.startswith("//"):
            src = "https:" + src
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


def _ensure_playwright_browsers() -> None:
    import os
    from pathlib import Path

    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if current and Path(current).exists():
        return
    local = Path.home() / "AppData" / "Local" / "ms-playwright"
    if local.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local)


def _open_browser():
    _ensure_playwright_browsers()
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
        json_dt = parse_posted_from_html(html)
        if has_time and visible_dt:
            posted_dt = visible_dt
            listing.extras["posted_has_time"] = True
        elif visible_dt:
            posted_dt = visible_dt
            listing.extras["posted_has_time"] = False
        elif json_dt:
            posted_dt = json_dt
            jlocal = _as_warsaw(json_dt)
            listing.extras["posted_has_time"] = not (
                jlocal.hour == 0 and jlocal.minute == 0
            )
        else:
            posted_dt = None
            listing.extras["posted_has_time"] = False
        if posted_dt:
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
        if "otodom.pl" in (listing.url or ""):
            _apply_otodom_json_prices(page, [listing])
        listing.details_loaded = True
    except Exception:
        logger.warning("detail skip %s", listing.id, exc_info=True)
    return listing


def fill_missing_czynsz(page, listings: list[Listing], *, limit: int = 12) -> None:
    """OLX картка майже ніколи не має чиншу — беремо зсередини оголошення."""
    opened = 0
    for item in listings:
        if opened >= limit:
            break
        if item.czynsz_zl is not None or item.details_loaded:
            continue
        if "facebook.com" in (item.url or ""):
            continue
        try:
            enrich_on_page(page, item)
            opened += 1
        except Exception:
            logger.warning("czynsz enrich skip %s", item.id, exc_info=True)


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
    seen_ids: dict[str, int] = {}
    fingerprints: dict[str, int] = {}
    for item in items:
        fp = re.sub(r"\W+", "", f"{item.title}{item.price}".lower())[:60]
        idx = seen_ids.get(item.id)
        if idx is None:
            idx = fingerprints.get(fp)
        if idx is not None:
            prev = unique[idx]
            if len(item.photos) > len(prev.photos):
                unique[idx] = item
                seen_ids[item.id] = idx
                fingerprints[fp] = idx
            continue
        seen_ids[item.id] = len(unique)
        fingerprints[fp] = len(unique)
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
