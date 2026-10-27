"""Generate test fixture images: a handwritten-style sales page and a till slip.

Usage: python scripts/make_fixtures.py
Vision is mocked in tests; these fixtures exercise the photo plumbing
(bytes in, hash, routing) and serve for manual live testing.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

BOOK_LINES = [
    "TUESDAY BOOK  21/10",
    "1  5 chap          5",
    "2  2 brd Sipho     40",
    "3  1 stny          34",
    "4  2 eggs Thandi   7",
    "5  Sipho paid 50",
    "   TOTAL 86",
]

SLIP_LINES = [
    "    JUMBO AMANZIMTOTI",
    "    TAX INVOICE  R-2210",
    "    21/10/2026  09:14",
    "CHAPPIES BOX100   85.00",
    "TASTIC 2KG x6    330.00",
    "COKE 2L x6       180.00",
    "             VAT  77.93",
    "           TOTAL 672.93",
    "CASH 672.93",
]


def _page(lines, ruled=True):
    img = Image.new("RGB", (600, 700), "white")
    d = ImageDraw.Draw(img)
    if ruled:
        for y in range(60, 700, 40):
            d.line([(20, y), (580, y)], fill=(200, 210, 230))
    y = 70
    for line in lines:
        d.text((40, y), line, fill=(20, 30, 80))
        y += 40
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    _page(BOOK_LINES).save(OUT / "book_page.png")
    _page(SLIP_LINES, ruled=False).save(OUT / "till_slip.png")
    print(f"wrote {OUT / 'book_page.png'} and {OUT / 'till_slip.png'}")


if __name__ == "__main__":
    main()
