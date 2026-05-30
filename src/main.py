#!/usr/bin/env python3
"""
Stake.com Esports Odds Scraper — v4 (2026-05-30)

Strategy: Pure GQL HTTP, no browser, no proxy, no auth.
- sportList           → all sport IDs (open, no auth)
- sport.tournamentList → tournament IDs per sport (open)
- sportTournament.fixtureList with groups → full odds incl. Match Winner (open!)

Key finding: markets { outcomes { odds } } requires auth, but
groups { templates { markets { outcomes { odds } } } } returns the SAME data
UNAUTHENTICATED. This is the SSR path Stake uses for public page render.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

import aiohttp
from apify import Actor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-gql")

GQL_URL = "https://stake.com/_api/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Origin": "https://stake.com",
    "Referer": "https://stake.com/sports/esports",
    "x-language": "en",
    "Accept-Language": "en-US,en;q=0.9",
}

# Esports sport slugs to include
ESPORTS_SLUGS = {
    "dota-2", "counter-strike", "league-of-legends", "valorant",
    "mobile-legends", "starcraft-2", "king-of-glory", "overwatch",
    "rocket-league", "rainbow-six", "warcraft-3",
}

# Market name patterns to treat as "Match Winner"
MATCH_WINNER_RE = re.compile(
    r"match winner|moneyline|1x2|match result|twoway|threeway",
    re.IGNORECASE,
)

QUERY_SPORT_LIST = "{ sportList { id name slug type } }"

QUERY_TOURNAMENT_LIST = """
query TournamentList($sportId: String!) {
  sport(sportId: $sportId) {
    tournamentList { id name }
  }
}
"""

# NOTE: eventStatus excluded — SportFixtureEventStatus requires inline fragment
# and we don't need it; status field on fixture is sufficient.
QUERY_FIXTURE_LIST = """
query FixtureList($tournamentId: String!) {
  sportTournament(tournamentId: $tournamentId) {
    id
    name
    fixtureList {
      id
      name
      slug
      status
      data {
        ... on SportFixtureDataMatch {
          startTime
          competitors { name }
        }
      }
      groups {
        name
        templates {
          name
          markets {
            id
            name
            status
            outcomes {
              id
              name
              odds
              active
            }
          }
        }
      }
    }
  }
}
"""


async def gql(session: aiohttp.ClientSession, query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    async with session.post(GQL_URL, json=payload) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"GQL HTTP {resp.status}: {text[:300]}")
        data = await resp.json()
        if "errors" in data:
            raise Exception(f"GQL errors: {data['errors']}")
        return data.get("data", {})


def extract_match_winner(fixture: dict, tournament_name: str, sport_slug: str, now: str) -> Optional[Dict]:
    """Extract Match Winner odds from fixture groups structure."""
    name = fixture.get("name", "")
    slug = fixture.get("slug", "")

    # Team names from data.competitors or parse from name
    fdata = fixture.get("data") or {}
    comps = [c.get("name", "") for c in fdata.get("competitors", [])]
    if not comps:
        parts = re.split(r"\s+(?:vs\.?|-)\s+", name, maxsplit=1)
        comps = [p.strip() for p in parts] if len(parts) == 2 else []

    if len(comps) < 2:
        return None
    team1, team2 = comps[0], comps[1]

    start_time = fdata.get("startTime", "")
    match_url = f"https://stake.com/sports/esports/{slug}" if slug else ""

    # Find best match winner market: prefer main group, prefer 2-way over 3-way
    best_market = None
    best_score = -1

    for group in fixture.get("groups", []):
        group_name = (group.get("name") or "").lower()
        group_priority = 3 if group_name == "main" else 2 if group_name in ("winner", "threeway") else 1

        for template in group.get("templates", []):
            for market in template.get("markets", []):
                if market.get("status") != "active":
                    continue
                mkt_name = market.get("name", "")
                if not MATCH_WINNER_RE.search(mkt_name):
                    continue
                outcomes = [o for o in market.get("outcomes", []) if o.get("active") and o.get("odds")]
                if len(outcomes) < 2:
                    continue
                score = group_priority * 10 - len(outcomes)
                if score > best_score:
                    best_score = score
                    best_market = (mkt_name, outcomes)

    if not best_market:
        return None

    mkt_name, outcomes = best_market
    p1 = p2 = p_draw = None

    for o in outcomes:
        o_name = (o.get("name") or "").lower()
        odds = float(o.get("odds", 0))
        if not (1.01 <= odds <= 500):
            continue
        if team1.lower() in o_name or o_name in ("home", "1"):
            p1 = odds
        elif team2.lower() in o_name or o_name in ("away", "2"):
            p2 = odds
        elif any(x in o_name for x in ("draw", "tie", "x")):
            p_draw = odds

    # Positional fallback when name matching fails
    if p1 is None and p2 is None:
        valid = [float(o["odds"]) for o in outcomes if 1.01 <= float(o.get("odds", 0)) <= 500]
        if len(valid) >= 2:
            p1 = valid[0]
            if len(valid) == 3:
                p_draw = valid[1]
                p2 = valid[2]
            else:
                p2 = valid[1]

    if p1 is None or p2 is None:
        return None

    return {
        "bookmaker": "stake",
        "game_raw": sport_slug,
        "tournament_name": tournament_name,
        "team1": team1,
        "team2": team2,
        "match_start_time": start_time,
        "match_url": match_url,
        "market_name": mkt_name,
        "price_team1": p1,
        "price_team2": p2,
        "price_draw": p_draw,
        "scraped_at": now,
    }


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_matches = input_data.get("maxMatches", 300)
        esports_slugs = set(input_data.get("esportsSlugs", list(ESPORTS_SLUGS)))

        actor.log.info(f"Stake GQL scraper v4 | max={max_matches} | sports={len(esports_slugs)}")

        now = datetime.now(timezone.utc).isoformat()
        final_records: List[Dict] = []
        seen_keys: set = set()

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:

            # Step 1: Get sport list, filter to esports
            actor.log.info("Fetching sport list...")
            sport_data = await gql(session, QUERY_SPORT_LIST)
            all_sports = sport_data.get("sportList", [])
            esports = [s for s in all_sports if s.get("slug") in esports_slugs]
            actor.log.info(f"Esports matched: {[s['slug'] for s in esports]}")

            if not esports:
                actor.log.error("No esports found in sportList")
                await actor.push_data({"error": "no_esports_found"})
                return

            # Step 2: For each esport, get tournaments
            for sport in esports:
                sport_name = sport["name"]
                sport_slug = sport["slug"]
                actor.log.info(f"Sport: {sport_name}")

                try:
                    t_data = await gql(session, QUERY_TOURNAMENT_LIST, {"sportId": sport["id"]})
                    tournaments = (t_data.get("sport") or {}).get("tournamentList", [])
                except Exception as e:
                    actor.log.warning(f"  tournamentList failed: {e}")
                    continue

                actor.log.info(f"  {len(tournaments)} tournaments")

                # Step 3: For each tournament, get fixtures + odds
                for t in tournaments:
                    try:
                        f_data = await gql(session, QUERY_FIXTURE_LIST, {"tournamentId": t["id"]})
                        t_obj = f_data.get("sportTournament") or {}
                        fixtures = t_obj.get("fixtureList", [])
                    except Exception as e:
                        actor.log.warning(f"    {t['name']}: {e}")
                        continue

                    t_records = 0
                    for fx in fixtures:
                        if fx.get("status") not in ("active", "live"):
                            continue
                        rec = extract_match_winner(fx, t["name"], sport_slug, now)
                        if not rec:
                            continue
                        key = f"{rec['team1'].lower()}||{rec['team2'].lower()}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        final_records.append(rec)
                        t_records += 1

                        if len(final_records) >= max_matches:
                            break

                    if t_records:
                        actor.log.info(f"    {t['name']}: {t_records} records")

                    if len(final_records) >= max_matches:
                        break

                    await asyncio.sleep(0.3)

                if len(final_records) >= max_matches:
                    actor.log.info(f"Reached max_matches={max_matches}, stopping")
                    break

                await asyncio.sleep(0.5)

        actor.log.info(f"Total unique records: {len(final_records)}")

        for rec in final_records:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True,
            "bookmaker": "stake",
            "records_total": len(final_records),
            "method": "gql_unauthenticated_groups",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
