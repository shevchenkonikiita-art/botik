"""Розрахунок повної місячної ціни: оренда + чинш + оплати (media)."""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_EXTRA_WHEN_UNKNOWN = 600
DEFAULT_CZYNSZ_WHEN_MENTIONED = 500
DEFAULT_MEDIA_WHEN_MENTIONED = 350

AMOUNT_RE = re.compile(
    r"(\d{1,2}(?:[ \u00a0]?\d{3})|\d{3,5})(?:[.,]\d+)?\s*(?:zł|zl|pln)?",
    re.IGNORECASE,
)

INCLUSIVE_HINTS = (
    "czynsz wliczony",
    "czynsz w cenie",
    "w cenie czynsz",
    "media wliczone",
    "media w cenie",
    "w cenie media",
    "wszystko wliczone",
    "wszystko w cenie",
    "cena zawiera",
    "zawiera czynsz",
    "zawiera media",
    "all inclusive",
    "all-in",
    "bez dodatkowych opłat",
    "bez dodatkowych oplat",
    "opłaty wliczone",
    "oplaty wliczone",
)

TOTAL_PATTERNS = (
    re.compile(
        r"(?:łącznie|lacznie|razem|total|całkowit(?:a|e|ie)?|calkowit(?:a|e|ie)?|"
        r"do zapłaty|do zaplaty)\s*[:=]?\s*"
        + AMOUNT_RE.pattern,
        re.IGNORECASE,
    ),
    re.compile(
        AMOUNT_RE.pattern
        + r"\s*(?:zł|zl|pln)?\s*(?:łącznie|lacznie|razem(?: z)?|z czynszem|z mediami)",
        re.IGNORECASE,
    ),
)

CZYNSZ_PATTERNS = (
    re.compile(
        r"czynsz(?:\s*administracyjny)?[^\d]{0,15}" + AMOUNT_RE.pattern,
        re.IGNORECASE,
    ),
    re.compile(
        AMOUNT_RE.pattern + r"[^\d]{0,10}czynsz(?:\s*administracyjny)?",
        re.IGNORECASE,
    ),
)

MEDIA_PATTERNS = (
    re.compile(
        r"(?:media|prąd|prad|gaz|woda|internet|opłaty|oplaty|eksploatac)"
        r"[^\d]{0,20}" + AMOUNT_RE.pattern,
        re.IGNORECASE,
    ),
    re.compile(
        AMOUNT_RE.pattern
        + r"[^\d]{0,10}(?:media|prąd|prad|opłat|oplat)",
        re.IGNORECASE,
    ),
)


def parse_amount(text: str) -> int | None:
    if not text:
        return None
    match = AMOUNT_RE.search(text.replace("\u00a0", " "))
    if not match:
        return None
    raw = re.sub(r"[^\d]", "", match.group(1).split(",")[0].split(".")[0])
    if not raw:
        return None
    value = int(raw)
    if value < 100 or value > 30000:
        return None
    return value


def _first_amount(patterns: tuple[re.Pattern[str], ...], text: str) -> int | None:
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        for group in match.groups():
            if group and re.search(r"\d", group):
                amount = parse_amount(group)
                if amount is not None:
                    return amount
        amount = parse_amount(match.group(0))
        if amount is not None:
            return amount
    return None


@dataclass
class PriceBreakdown:
    base: int | None
    czynsz: int
    media: int
    total: int | None
    inclusive: bool
    estimated: bool
    note: str

    def format_line(self) -> str:
        """Короткий рядок у стилі Warsaw Rent."""
        if self.total is None:
            return "💰 ціна невідома"
        if self.inclusive and not self.estimated:
            return f"💰 {self.total} zł/міс · усе включено"
        if self.estimated:
            return f"💰 ≈{self.total} zł/міс · орієнтовно з чиншем і оплатами"
        parts = []
        if self.czynsz:
            parts.append(f"чинш {self.czynsz}")
        if self.media:
            parts.append(f"оплати {self.media}")
        extra = f" · {' + '.join(parts)}" if parts else ""
        return f"💰 {self.total} zł/міс (оренда {self.base}{extra})"


def analyze_price(listed_price_text: str, description: str = "") -> PriceBreakdown:
    blob = f"{listed_price_text}\n{description}".lower().replace("\u00a0", " ")
    base = parse_amount(listed_price_text)

    explicit_total = _first_amount(TOTAL_PATTERNS, blob)
    if explicit_total is not None:
        return PriceBreakdown(
            base=base,
            czynsz=0,
            media=0,
            total=explicit_total,
            inclusive=True,
            estimated=False,
            note="explicit_total",
        )

    inclusive = any(h in blob for h in INCLUSIVE_HINTS)
    czynsz = _first_amount(CZYNSZ_PATTERNS, blob) or 0
    media = _first_amount(MEDIA_PATTERNS, blob) or 0

    mentions_czynsz = bool(re.search(r"czynsz|\+ ?czynsz", blob))
    mentions_media = bool(
        re.search(r"\+ ?media|media osobno|media około|media ok", blob)
    )

    estimated = False
    if inclusive:
        czynsz = 0
        media = 0
    else:
        if czynsz == 0 and mentions_czynsz:
            czynsz = DEFAULT_CZYNSZ_WHEN_MENTIONED
            estimated = True
        if media == 0 and mentions_media:
            media = DEFAULT_MEDIA_WHEN_MENTIONED
            estimated = True
        if (
            base is not None
            and czynsz == 0
            and media == 0
            and not inclusive
            and not mentions_czynsz
            and not mentions_media
        ):
            media = DEFAULT_EXTRA_WHEN_UNKNOWN
            estimated = True

    if base is None:
        return PriceBreakdown(
            base=None,
            czynsz=czynsz,
            media=media,
            total=None,
            inclusive=inclusive,
            estimated=estimated,
            note="no_base",
        )

    total = base if inclusive else base + czynsz + media
    return PriceBreakdown(
        base=base,
        czynsz=czynsz,
        media=media,
        total=total,
        inclusive=inclusive,
        estimated=estimated,
        note="ok",
    )


def fits_budget(
    breakdown: PriceBreakdown,
    min_price: int,
    max_price: int,
) -> bool:
    if breakdown.total is None:
        return False
    if min_price > 0 and breakdown.total < min_price:
        return False
    if max_price < 90000 and breakdown.total > max_price:
        return False
    return True
