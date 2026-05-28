#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper

ARCHITECTURE (2026-05-28 v3):

CF solved in 1s via Oxylabs residential proxy + stealth v2 ✓
GQL interception race condition fixed: listener attached BEFORE page.goto()
DOM scraper rewritten: parses Stake's actual card structure from observed logs

Stake page card structure (observed from logs):
  Line 0: Game name (e.g. "League of Legends", "CS2")
  Line 1: Status ("Live" or time like "1m 6s")
  Line 2: Map info ("5th Map", "1st Map") -- may be absent on pre-match
  Line 3: Tournament name ("NACL 2026 Spring", "Circuit X Base Recife")
  Line 4: Team 1 name
  Line 5: Team 2 name
  Lines 6-9: Score digits (live) or absent (pre-match)
  Last meaningful line before odds: "Match Winner - Twoway/Threeway" label
  Odds: two or three decimal numbers

Strategy:
1. Attach response listener BEFORE page.goto()
2. Navigate each sport page (CS2, LoL, Dota2, Valorant, etc.) — GQL fires on each
3. Parse intercepted GQL responses (unauthenticated = odds in DOM, not GQL)
4. DOM fallback: parse card structure properly using the observed line pattern
5. Navigate to individual game pages to get more cards beyond the hub
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from apify import Actor
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-scraper")

DEFAULT_PROXY = {
    "server": "http://pr.oxylabs.io:7777",
    "username": "customer-sonus_TbxLY-cc-ca-city-edmonton",
    "password": "***",
}

STAKE_BASE = "https://stake.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Stake esports game slugs — we navigate each to trigger GQL and collect more cards
ESPORTS_GAME_SLUGS = [
    "dota-2",
    "counter-strike",
    "league-of-legends",
    "valorant",
    "mobile-legends",
    "starcraft-2",
    "king-of-glory",
    "overwatch",
    "rocket-league",
    "fifa",
]


def parse_decimal_odds(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v if 1.01 <= v <= 500 else None
    t = str(val).strip().replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", t)
    if m:
        v = float(m.group(1))
        return v if 1.01 <= v <= 500 else None
    return None


def safe_kv_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def handle_cf_challenge(page, max_wait_s: int = 90) -> bool:
    for i in range(max_wait_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            cur_url = page.url
        except Exception:
            continue
        if i % 15 == 0:
            logger.info(f"CF wait {i+1}s | title={title!r}")
        if (
            title != "Just a moment..."
            and "__cf_chl" not in cur_url
            and "challenge" not in cur_url.lower()
            and "cf-challenge" not in title.lower()
        ):
            logger.info(f"CF resolved at {i+1}s")
            await asyncio.sleep(3)
            return True
    return False


def extract_records_from_graphql(body: dict, now: str) -> List[Dict]:
    """Parse intercepted GQL response. Stake returns odds in outcomes when authenticated."""
    records = []
    data = body.get("data") or {}

    def parse_fixture(fixture: dict, t_name: str, game_name: str) -> List[Dict]:
        recs = []
        name = fixture.get("name", "")
        start_time = fixture.get("startTime", "")
        slug = fixture.get("slug", "")
        match_url = f"{STAKE_BASE}/sports/esports/{slug}" if slug else ""

        # Team names from "Team A vs Team B" or "Team A - Team B"
        vs_parts = re.split(r"\s+(?:vs\.?|VS\.?|–|-)\s+", name, maxsplit=1)
        team1 = vs_parts[0].strip() if len(vs_parts) == 2 else ""
        team2 = vs_parts[1].strip() if len(vs_parts) == 2 else ""

        target_markets = {"match winner", "match result", "winner", "1x2", "moneyline"}
        for market in fixture.get("markets", []):
            if not isinstance(market, dict):
                continue
            mkt_name = (market.get("name") or "").lower()
            if mkt_name and mkt_name not in target_markets:
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            p1 = p2 = p_draw = None
            for o in outcomes:
                o_name = (o.get("name") or "").lower()
                odds = parse_decimal_odds(o.get("odds"))
                if not odds:
                    continue
                if team1 and team1.lower() in o_name:
                    p1 = odds
                elif team2 and team2.lower() in o_name:
                    p2 = odds
                elif any(x in o_name for x in ("draw", "tie")):
                    p_draw = odds
                elif o_name in ("home", "1") and p1 is None:
                    p1 = odds
                elif o_name in ("away", "2") and p2 is None:
                    p2 = odds
            if p1 or p2:
                recs.append({
                    "bookmaker": "stake",
                    "game_raw": game_name,
                    "tournament_name": t_name,
                    "team1": team1,
                    "team2": team2,
                    "match_start_time": start_time,
                    "match_url": match_url,
                    "price_team1": p1,
                    "price_team2": p2,
                    "price_draw": p_draw,
                    "scraped_at": now,
                })
        return recs

    # sportTournament.fixtureList path
    t = data.get("sportTournament")
    if t:
        for fx in t.get("fixtureList", []):
            records.extend(parse_fixture(fx, t.get("name", ""), ""))

    # Direct fixtureList / events at top level
    for key in ("fixtureList", "fixtures", "events"):
        if key in data and isinstance(data[key], list):
            for fx in data[key]:
                records.extend(parse_fixture(fx, "", ""))

    return records


def parse_card_lines(lines: list) -> Optional[Dict]:
    """
    Parse Stake card innerText lines into a structured record.

    Observed structure from logs:
      [0] game name: "League of Legends" / "CS2" / "FIFA"
      [1] status: "Live" or countdown like "1m 6s" or date
      [2] (optional) map: "5th Map" / "1st Map" — skip if starts with digit+letter
      [N] tournament name
      [N+1] team1 name
      [N+2] team2 name
      [N+3..] score digits (if live): "2", "2", "0", "1"
      then: "Match Winner - Twoway" or "Match Winner - Threeway" label
      then: odds values as decimal strings

    We extract: game, tournament, team1, team2, and find decimal odds in the lines.
    """
    if len(lines) < 5:
        return None

    # Find all decimal odds in the lines (format X.XX or XX.XX)
    odds_values = []
    odds_indices = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\d{1,3}\.\d{2,3})$", line.strip())
        if m:
            v = float(m.group(1))
            if 1.01 <= v <= 500:
                odds_values.append(v)
                odds_indices.append(i)

    if len(odds_values) < 2:
        return None

    # Find "Match Winner" label to anchor team extraction
    mw_idx = None
    for i, line in enumerate(lines):
        if "match winner" in line.lower() or "moneyline" in line.lower():
            mw_idx = i
            break

    # Teams are the two lines immediately before "Match Winner" label,
    # after skipping score digits (single digit lines)
    team1, team2, tournament = "", "", ""

    if mw_idx and mw_idx >= 3:
        # Work backwards from Match Winner label, skip score digits
        candidates = []
        i = mw_idx - 1
        while i >= 2 and len(candidates) < 4:
            line = lines[i].strip()
            # Score digit: single or double digit alone
            if re.match(r"^\d{1,2}$", line):
                i -= 1
                continue
            candidates.insert(0, (i, line))
            i -= 1

        # Last two candidates = team2, team1
        if len(candidates) >= 2:
            team1 = candidates[-2][1]
            team2 = candidates[-1][1]
            # tournament = line just before team1 if it exists
            t_idx = candidates[-2][0] - 1
            if t_idx >= 2:
                potential_t = lines[t_idx].strip()
                # Skip if it looks like a map indicator
                if not re.match(r"^\d+(st|nd|rd|th)\s+Map", potential_t, re.I):
                    tournament = potential_t
        elif len(candidates) == 1:
            team2 = candidates[0][1]

    # Game name is line 0
    game_raw = lines[0].strip()

    # Determine if live or upcoming
    status_line = lines[1].strip() if len(lines) > 1 else ""

    # Draw odds: if "Threeway" or "Draw" label in lines, treat 3rd odds as draw
    p_draw = None
    has_draw = any("threeway" in l.lower() or "draw" in l.lower() for l in lines)
    if len(odds_values) >= 3 and has_draw:
        # Stake threeway shows: team1_odds, draw_odds, team2_odds
        # But from the FIFA example: 1.62, 4.50, 2.70
        # and output had team1=1.62, team2=4.5 (draw), price_draw=2.70
        # The label order on page is: Home | Draw | Away
        p1 = odds_values[0]
        p_draw = odds_values[1]
        p2 = odds_values[2]
    elif len(odds_values) >= 2:
        p1 = odds_values[0]
        p2 = odds_values[1]
    else:
        return None

    if not team1 and not team2:
        return None

    return {
        "game_raw": game_raw,
        "tournament_name": tournament,
        "team1": team1,
        "team2": team2,
        "status": status_line,
        "price_team1": p1,
        "price_team2": p2,
        "price_draw": p_draw,
    }


async def scrape_page_dom(page, now: str, match_url: str) -> List[Dict]:
    """
    DOM scrape of the current page. Finds match cards and parses team/odds.
    Uses innerText of each card — Stake renders full card text as observable lines.
    """
    records = []
    try:
        # Get all card-level containers that contain odds
        cards_data = await page.evaluate("""() => {
            const cards = [];

            // Strategy: find elements containing 2+ decimal odds values
            // Stake cards are typically div>div>a or similar — walk all leaf containers
            const candidates = document.querySelectorAll(
                'a[href*="/sports/"], [class*="event"], [class*="match"], [class*="fixture"], ' +
                '[class*="Event"], [class*="Match"], article, [role="listitem"]'
            );

            const seen = new Set();
            candidates.forEach(el => {
                const text = el.innerText || '';
                const oddsMatches = text.match(/\\b\\d{1,3}\\.\\d{2,3}\\b/g) || [];
                const validOdds = oddsMatches.filter(o => {
                    const v = parseFloat(o);
                    return v >= 1.01 && v <= 500;
                });
                if (validOdds.length >= 2 && !seen.has(text.substring(0,100))) {
                    seen.add(text.substring(0,100));
                    const href = el.tagName === 'A' ? el.href : (el.querySelector('a') ? el.querySelector('a').href : '');
                    cards.push({
                        text: text.substring(0, 800),
                        href: href,
                        odds: validOdds
                    });
                }
            });

            // Also try: find ALL decimal numbers on page grouped by proximity
            if (cards.length === 0) {
                // Fallback: just grab all text blobs with 2+ odds
                document.querySelectorAll('div, section').forEach(el => {
                    if (el.children.length > 0 && el.children.length < 30) {
                        const text = el.innerText || '';
                        const oddsMatches = text.match(/\\b\\d{1,3}\\.\\d{2,3}\\b/g) || [];
                        const validOdds = oddsMatches.filter(o => {
                            const v = parseFloat(o);
                            return v >= 1.01 && v <= 500;
                        });
                        if (validOdds.length >= 2 && validOdds.length <= 6 && text.length < 600) {
                            if (!seen.has(text.substring(0,100))) {
                                seen.add(text.substring(0,100));
                                cards.push({text: text.substring(0,600), href: '', odds: validOdds});
                            }
                        }
                    }
                });
            }

            return cards.slice(0, 100);
        }""")

        logger.info(f"DOM: found {len(cards_data)} card candidates on {page.url}")

        for card in cards_data:
            raw_text = card.get("text", "")
            href = card.get("href", "") or match_url
            lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

            parsed = parse_card_lines(lines)
            if not parsed:
                # Log for debugging so we can improve the parser
                logger.debug(f"  Unparsed card: {raw_text[:150]}")
                continue

            # Prefer card's own href as match URL
            card_url = href if href else match_url

            records.append({
                "bookmaker": "stake",
                "game_raw": parsed["game_raw"],
                "tournament_name": parsed["tournament_name"],
                "team1": parsed["team1"],
                "team2": parsed["team2"],
                "match_start_time": parsed.get("status", ""),
                "match_url": card_url,
                "price_team1": parsed["price_team1"],
                "price_team2": parsed["price_team2"],
                "price_draw": parsed["price_draw"],
                "scraped_at": now,
            })

    except Exception as e:
        logger.error(f"DOM scrape error: {e}")

    return records


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_url = input_data.get("proxyUrl") or os.environ.get("OXYLABS_PROXY") or DEFAULT_PROXY
        stake_base = input_data.get("stakeBaseUrl") or STAKE_BASE
        max_matches = input_data.get("maxMatches", 200)
        headless = input_data.get("headless", False)

        actor.log.info(
            f"Stake scraper v3 | base={stake_base} headless={headless} "
            f"stealth={'v2' if STEALTH_AVAILABLE else 'MISSING'}"
        )

        now = datetime.now(timezone.utc).isoformat()
        intercepted_records: List[Dict] = []
        seen_keys: set = set()  # dedup by team1+team2

        launch_args = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-automation",
            ]
        }

        if proxy_url:
            launch_args["proxy"] = proxy_url if isinstance(proxy_url, dict) else {"server": proxy_url}
            actor.log.info("Proxy: Oxylabs residential CA")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-CA",
                timezone_id="America/Edmonton",
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "en-CA,en;q=0.9",
                    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )

            if STEALTH_AVAILABLE:
                await Stealth().apply_stealth_async(context)
                actor.log.info("Stealth v2 applied ✓")

            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            # ── ATTACH RESPONSE LISTENER BEFORE ANY NAVIGATION ───────────
            # Critical: must be attached before goto() or initial GQL requests are missed
            async def on_response(response):
                try:
                    if "/_api/graphql" not in response.url:
                        return
                    if response.status != 200:
                        return
                    body = await response.json()
                    recs = extract_records_from_graphql(body, now)
                    if recs:
                        logger.info(f"GQL intercept: {len(recs)} records from {response.url}")
                        intercepted_records.extend(recs)
                    else:
                        # Log what operation returned for debugging
                        try:
                            req_data = await response.request.post_data_json()
                            op = req_data.get("operationName") if isinstance(req_data, dict) else None
                            data_keys = list((body.get("data") or {}).keys())
                            logger.info(f"GQL op={op} | data_keys={data_keys} | no odds extracted")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Response handler: {e}")

            page.on("response", on_response)
            # ─────────────────────────────────────────────────────────────

            # Navigate esports hub first
            hub_url = f"{stake_base}/sports/esports"
            actor.log.info(f"Navigating hub: {hub_url}")
            try:
                await page.goto(hub_url, wait_until="commit", timeout=30000)
            except Exception as e:
                actor.log.error(f"Hub navigation failed: {e}")
                await browser.close()
                await actor.push_data({"error": "navigation_failed", "message": str(e)})
                return

            cf_ok = await handle_cf_challenge(page)
            if not cf_ok:
                actor.log.error("CF not solved")
                try:
                    html = await page.content()
                    await Actor.set_value("debug_cf_html", html, content_type="text/html")
                    shot = await page.screenshot(full_page=True, timeout=15000)
                    await Actor.set_value("debug_cf_screenshot", shot, content_type="image/png")
                except Exception:
                    pass
                await browser.close()
                await actor.push_data({"error": "cf_not_solved"})
                return

            # Wait for SPA to fully hydrate
            actor.log.info("CF solved — waiting for SPA hydration")
            await asyncio.sleep(6)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

            # Save debug HTML + screenshot of hub page
            try:
                shot = await page.screenshot(full_page=True, timeout=15000)
                await Actor.set_value("debug_hub_screenshot", shot, content_type="image/png")
            except Exception:
                pass

            # DOM scrape hub page
            all_records: List[Dict] = []
            hub_records = await scrape_page_dom(page, now, hub_url)
            actor.log.info(f"Hub DOM: {len(hub_records)} records")
            all_records.extend(hub_records)

            # Navigate each game slug page for more cards
            actor.log.info(f"Drilling {len(ESPORTS_GAME_SLUGS)} game pages...")
            for slug in ESPORTS_GAME_SLUGS:
                game_url = f"{stake_base}/sports/esports/{slug}"
                try:
                    actor.log.info(f"  → {slug}")
                    await page.goto(game_url, wait_until="commit", timeout=20000)
                    # No new CF expected — cookies persist
                    await asyncio.sleep(4)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    game_records = await scrape_page_dom(page, now, game_url)
                    actor.log.info(f"    {slug}: {len(game_records)} records")
                    all_records.extend(game_records)

                except Exception as e:
                    logger.warning(f"  {slug} failed: {e}")
                    continue

            actor.log.info(f"GQL intercepted: {len(intercepted_records)} | DOM scraped: {len(all_records)}")

            # Merge: prefer GQL records (have start_time), deduplicate by team pair
            final_records: List[Dict] = []
            for rec in (intercepted_records + all_records):
                key = f"{rec.get('team1','').lower()}||{rec.get('team2','').lower()}"
                if key and key not in seen_keys and rec.get("team1") and rec.get("team2"):
                    seen_keys.add(key)
                    final_records.append(rec)
                elif not rec.get("team1") and not rec.get("team2"):
                    pass  # skip empty

            final_records = final_records[:max_matches]
            actor.log.info(f"Final unique records: {len(final_records)}")

            await browser.close()

        for rec in final_records:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True,
            "bookmaker": "stake",
            "records_total": len(final_records),
            "gql_records": len(intercepted_records),
            "dom_records": len(all_records),
            "method": "gql+dom",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
