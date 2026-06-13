#!/usr/bin/env python3
"""
Stake.com Esports Odds Scraper — v7 (2026-06-13)

Schema: SCHEMA-LOCK-2026-06-07.md — all actors must conform.
Changes in v7:
  - game_raw extracted from tournament.category.sport.name (actual game, not "International")
  - game field added (canonical via normalise_game, with raw fallback)
  - pagination made more resilient: retry on failure, rotate proxy only when banned

Uses fixtureList(sportType:esport, limit:50, offset:N) to get all esports
fixtures across paginated calls. Oxylabs residential proxy for CF bypass.
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

import aiohttp
from apify import Actor
from src.normalise import normalise_game

GQL_URL = "https://stake.com/_api/graphql"

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Origin": "https://stake.com",
    "Referer": "https://stake.com/sports/esports",
    "x-language": "en",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Google Chrome";v="135", "Not-A.Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

PROXY_LIST = [
    # Stake esport accepts: CA(Edmonton), IE(Dublin), FI(Helsinki), NO(Oslo), NZ(Auckland),
    #     JP(Tokyo), PL(Warsaw), HU(Budapest), IS(Reykjavik), LU(Luxembourg), SG
    # NOT accepted: US, GB, FR, DE, NL, AU, SE, AT, BE, CH, ES, IT, DK
    "http://customer-sonus_TbxLY-cc-ca-city-edmonton:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-ie-city-dublin:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-fi-city-helsinki:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-no-city-oslo:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-nz-city-auckland:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-jp-city-tokyo:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-pl-city-warsaw:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-hu-city-budapest:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-is-city-reykjavik:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-lu:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://numbnuts_9kOSG:~SWmnT7Qe~n7Fi@pr.oxylabs.io:7777",
]

MATCH_WINNER_RE = re.compile(
    r"match winner|moneyline|1x2|match result|twoway|threeway",
    re.IGNORECASE,
)

Q_FIXTURE_PAGE = """
query EsportsPage($offset: Int!) {
  fixtureList(sportType: esport, limit: 50, offset: $offset) {
    id
    name
    slug
    status
    tournament {
      name
      category {
        id
        name
        slug
        sport { id name slug }
      }
    }
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
"""


async def gql(session: aiohttp.ClientSession, query: str, variables: dict, proxy: str) -> dict:
    async with session.post(
        GQL_URL,
        json={"query": query, "variables": variables},
        proxy=proxy,
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"HTTP {resp.status}: {text[:200]}")
        data = await resp.json()
        if "errors" in data:
            raise Exception(f"GQL errors: {data['errors']}")
        return data.get("data", {})


def extract_match_winner(fixture: dict, now: str) -> Optional[Dict]:
    name = fixture.get("name", "")
    slug = fixture.get("slug", "")
    fdata = fixture.get("data") or {}
    tournament = fixture.get("tournament") or {}
    category = (tournament.get("category") or {})
    sport_info = category.get("sport") or {}

    # The actual esport title lives on the sport node, NOT category.name.
    # category.name is usually "International" / regional, not the game.
    game_raw = sport_info.get("name", "") or category.get("name", "")
    sport_slug = sport_info.get("slug", "")
    t_name = tournament.get("name", "")

    comps = [c.get("name", "") for c in fdata.get("competitors", [])]
    if not comps:
        parts = re.split(r"\s+(?:vs\.?|-)\s+", name, maxsplit=1)
        comps = [p.strip() for p in parts] if len(parts) == 2 else []
    if len(comps) < 2:
        return None
    team1, team2 = comps[0], comps[1]

    start_time = fdata.get("startTime", "")
    match_url = f"https://stake.com/sports/esports/{slug}" if slug else ""

    best_market = None
    best_score = -1
    for group in fixture.get("groups", []):
        gn = (group.get("name") or "").lower()
        gp = 3 if gn == "main" else 2 if gn in ("winner", "threeway") else 1
        for tmpl in group.get("templates", []):
            for mkt in tmpl.get("markets", []):
                if mkt.get("status") != "active":
                    continue
                if not MATCH_WINNER_RE.search(mkt.get("name", "")):
                    continue
                outs = [o for o in mkt.get("outcomes", []) if o.get("active") and o.get("odds")]
                if len(outs) < 2:
                    continue
                score = gp * 10 - len(outs)
                if score > best_score:
                    best_score = score
                    best_market = (mkt["name"], outs)

    if not best_market:
        return None
    mkt_name, outcomes = best_market
    p1 = p2 = p_draw = None
    for o in outcomes:
        on = (o.get("name") or "").lower()
        odds = float(o.get("odds", 0))
        if not (1.01 <= odds <= 500):
            continue
        if team1.lower() in on or on in ("home", "1"):
            p1 = odds
        elif team2.lower() in on or on in ("away", "2"):
            p2 = odds
        elif any(x in on for x in ("draw", "tie")):
            p_draw = odds
    if p1 is None and p2 is None:
        valid = [float(o["odds"]) for o in outcomes if 1.01 <= float(o.get("odds", 0)) <= 500]
        if len(valid) >= 2:
            p1 = valid[0]
            if len(valid) == 3:
                p_draw, p2 = valid[1], valid[2]
            else:
                p2 = valid[1]
    if p1 is None or p2 is None:
        return None

    canonical_game = normalise_game(game_raw)
    return {
        "bookmaker": "stake",
        "game_raw": game_raw,
        "sport_slug": sport_slug,
        "game": canonical_game if canonical_game else game_raw,
        "tournament_name": t_name,
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


_proxy_state = {"idx": 0}


async def gql_with_retry(session: aiohttp.ClientSession, query: str, variables: dict, actor, max_attempts: int = 3) -> dict:
    """Run a GQL query; rotate to next proxy on CF block / HTTP error."""
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        proxy_idx = _proxy_state["idx"]
        proxy = PROXY_LIST[proxy_idx % len(PROXY_LIST)]
        actor.log.info(f"GQL attempt {attempt+1}/{max_attempts} offset={variables.get('offset')} via proxy #{proxy_idx % len(PROXY_LIST)}")
        try:
            data = await gql(session, query, variables, proxy)
            # proxy worked — keep using it for the following requests
            return data
        except Exception as exc:
            last_err = exc
            msg = str(exc)
            actor.log.warning(f"  proxy failed: {msg[:120]}")
            # rotate proxy and retry
            _proxy_state["idx"] = (proxy_idx + 1) % len(PROXY_LIST)
            await asyncio.sleep(min(2 ** attempt, 10))
    raise last_err


async def main() -> None:
    async with Actor() as actor:
        inp = await actor.get_input() or {}
        max_matches = inp.get("maxMatches", 500)

        now = datetime.now(timezone.utc).isoformat()
        records: List[Dict] = []
        seen: set = set()

        actor.log.info(f"Stake GQL v7 | schema-locked | max={max_matches} | paginated fixtureList(sportType:esport)")

        conn = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=BASE_HEADERS, connector=conn, timeout=timeout) as session:
            offset = 0
            page = 0
            total_fixtures = 0
            while len(records) < max_matches:
                try:
                    data = await gql_with_retry(session, Q_FIXTURE_PAGE, {"offset": offset}, actor)
                except Exception as exc:
                    actor.log.error(f"Gave up after retries at offset {offset}: {exc}")
                    break

                fixtures = data.get("fixtureList", [])
                if not fixtures:
                    actor.log.info("Empty page — done")
                    break

                total_fixtures += len(fixtures)
                page_records = 0
                by_game: dict = {}
                for fx in fixtures:
                    rec = extract_match_winner(fx, now)
                    if not rec:
                        continue
                    key = f"{rec['team1'].lower()}||{rec['team2'].lower()}"
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(rec)
                    page_records += 1
                    sport = rec["game"] or rec["game_raw"]
                    by_game[sport] = by_game.get(sport, 0) + 1

                actor.log.info(f"  page {page+1}: {page_records}/{len(fixtures)} usable | by_game={by_game}")

                if len(fixtures) < 50:
                    break

                offset += 50
                page += 1
                await asyncio.sleep(0.5)

        actor.log.info(f"Total fixtures seen: {total_fixtures} | extracted: {len(records)}")
        for rec in records:
            await actor.push_data(rec)
        await actor.push_data({
            "_meta": True, "bookmaker": "stake",
            "records_total": len(records),
            "fixtures_seen": total_fixtures,
            "method": "gql_fixture_list_paginated",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
