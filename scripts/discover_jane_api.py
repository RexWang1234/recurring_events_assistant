"""
Jane App automated API discovery.

Navigates the booking flow automatically, captures all API responses,
and dumps both the page text and API data so we can build a reliable scraper.

Usage:
    python3 scripts/discover_jane_api.py
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BOOKING_URL = "https://mobilitypluschiropractic.janeapp.com/"
OUTPUT_DIR = Path(__file__).parent


async def main():
    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture ALL JSON responses — no CSS blocking so page renders properly
        async def capture(response):
            if "janeapp" not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                captured.append({"url": response.url, "data": body})
                print(f"  [API] {response.url}")
            except Exception:
                pass

        page.on("response", capture)

        # ── Step 1: Load page ──────────────────────────────────────────────────
        print("\n→ Loading page...")
        await page.goto(BOOKING_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        print("\n── Page text (first 3000 chars) ──────────────────────────────")
        text = await page.inner_text("body")
        print(text[:3000])

        # Save full page text
        (OUTPUT_DIR / "jane_page_text.txt").write_text(text)

        # ── Step 2: Click Registered Massage Therapy ───────────────────────────
        print("\n→ Looking for massage service...")
        for kw in ["Registered Massage", "Massage Therapy", "Massage"]:
            try:
                el = page.get_by_text(kw, exact=False).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    print(f"  Clicked: '{kw}'")
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # Save page text after clicking service
        text2 = await page.inner_text("body")
        (OUTPUT_DIR / "jane_page_after_service.txt").write_text(text2)
        print("\n── Page text after service click (first 3000 chars) ──────────")
        print(text2[:3000])

        # ── Step 3: Click first available practitioner ─────────────────────────
        print("\n→ Looking for a practitioner...")
        for kw in ["Book Now", "Select", "Upkar", "Registered Massage Therapist"]:
            try:
                el = page.get_by_text(kw, exact=False).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    print(f"  Clicked: '{kw}'")
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # Save page text after clicking practitioner
        text3 = await page.inner_text("body")
        (OUTPUT_DIR / "jane_page_after_practitioner.txt").write_text(text3)
        print("\n── Page text after practitioner click (first 3000 chars) ─────")
        print(text3[:3000])

        # ── Step 4: Wait for calendar and capture ──────────────────────────────
        await page.wait_for_timeout(3000)
        text4 = await page.inner_text("body")
        (OUTPUT_DIR / "jane_page_calendar.txt").write_text(text4)
        print("\n── Calendar page text (first 3000 chars) ─────────────────────")
        print(text4[:3000])

        await browser.close()

    # Save all captured API calls
    (OUTPUT_DIR / "jane_api_calls.json").write_text(
        json.dumps(captured, indent=2, default=str)
    )

    print(f"\n\n{'='*60}")
    print(f"Captured {len(captured)} API calls:")
    for c in captured:
        print(f"  {c['url']}")

    print(f"\nFiles saved to {OUTPUT_DIR}:")
    print("  jane_api_calls.json")
    print("  jane_page_text.txt")
    print("  jane_page_after_service.txt")
    print("  jane_page_after_practitioner.txt")
    print("  jane_page_calendar.txt")


if __name__ == "__main__":
    asyncio.run(main())
