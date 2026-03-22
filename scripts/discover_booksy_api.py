"""
Booksy API discovery script.

Navigates the Booksy booking page, captures all API calls, and saves them
so we can build a reliable Booksy-specific scraper (like jane_app_scraper.py).

Usage:
    python3 scripts/discover_booksy_api.py
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BOOKING_URL = "https://booksy.com/en-ca/21441_nex-barbershop_barbershop_773207_oakville?rwg_token=AFd1xnFg99B6BpgFQ1IA5Zehmjir1n8LK15cQGYZnC61gYPa-tnzzdnPqOhXqxZo7yVuEC_XmwuBFtvE6G_ZOWRNj2Ve5VWT8w%3D%3D#ba_s=seo"
OUTPUT_DIR = Path(__file__).parent


async def main():
    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        async def capture(response):
            if "booksy" not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            if response.status != 200:
                return
            try:
                body = await response.json()
                captured.append({"url": response.url, "status": response.status, "data": body})
                print(f"  [API] {response.url}")
            except Exception:
                pass

        page.on("response", capture)

        # ── Step 1: Load page ──────────────────────────────────────────────
        print(f"\n→ Loading {BOOKING_URL}")
        try:
            await page.goto(BOOKING_URL, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"  (networkidle timeout — continuing) {e}")
        await page.wait_for_timeout(3000)

        # Save initial page text
        text = await page.inner_text("body")
        (OUTPUT_DIR / "booksy_page_text.txt").write_text(text)
        print("\n── Page text (first 3000 chars) ──────────────────────────────")
        print(text[:3000])

        # ── Step 2: Look for booking triggers ─────────────────────────────
        print("\n→ Looking for booking triggers...")
        for kw in ["Book Now", "Book Appointment", "Book", "Schedule", "See Times", "Haircut"]:
            try:
                el = page.get_by_text(kw, exact=False).first
                if await el.is_visible(timeout=3000):
                    await el.click()
                    print(f"  Clicked: '{kw}'")
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        text2 = await page.inner_text("body")
        (OUTPUT_DIR / "booksy_page_after_click.txt").write_text(text2)
        print("\n── Page text after click (first 3000 chars) ──────────────────")
        print(text2[:3000])

        # ── Step 3: Wait for calendar / availability to load ───────────────
        await page.wait_for_timeout(3000)
        text3 = await page.inner_text("body")
        (OUTPUT_DIR / "booksy_page_calendar.txt").write_text(text3)

        await browser.close()

    # Save all captured API calls
    (OUTPUT_DIR / "booksy_api_calls.json").write_text(
        json.dumps(captured, indent=2, default=str)
    )

    print(f"\n\n{'='*60}")
    print(f"Captured {len(captured)} API calls:")
    for c in captured:
        print(f"  [{c['status']}] {c['url']}")

    print(f"\nFiles saved to {OUTPUT_DIR}:")
    print("  booksy_api_calls.json")
    print("  booksy_page_text.txt")
    print("  booksy_page_after_click.txt")
    print("  booksy_page_calendar.txt")
    print("\nShare booksy_api_calls.json to build a Booksy-specific scraper.")


if __name__ == "__main__":
    asyncio.run(main())
