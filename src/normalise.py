import logging
from rapidfuzz import process, fuzz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CANONICAL_GAMES = [
    "League of Legends",
    "Dota 2",
    "Dota 2 Duels",
    "CS2",
    "CS2 Duels",
    "PUBG",
    "Valorant",
    "Apex Legends",
    "Call of Duty",
    "Fortnite",
    "Mobile Legends: Bang Bang",
    "Overwatch 2",
    "Rainbow Six Siege",
    "Rocket League",
    "Street Fighter 6",
    "Tekken 8",
    "FIFA",
    "FIFA Bots",
    "NBA2K",
    "Crossfire",
    "King of Glory",
    "eCricket",
]

KNOWN_ALIASES = {
    # CS2
    "Counter-Strike 2": "CS2",
    "CS:GO": "CS2",
    "CSGO": "CS2",
    "Counter-Strike": "CS2",
    "CS2 Duels": "CS2 Duels",
    "Counter-Strike Duels": "CS2 Duels",
    # Dota
    "DOTA 2": "Dota 2",
    "Dota2": "Dota 2",
    "DOTA2": "Dota 2",
    "Dota 2 Duels": "Dota 2 Duels",
    # LoL
    "LoL": "League of Legends",
    # PUBG
    "PUBG: Battlegrounds": "PUBG",
    # Valorant
    "VALORANT": "Valorant",
    # Call of Duty
    "COD": "Call of Duty",
    "Warzone": "Call of Duty",
    "Black Ops 7": "Call of Duty",
    # Overwatch
    "Overwatch": "Overwatch 2",
    "OW2": "Overwatch 2",
    # Rainbow Six
    "Rainbow Six": "Rainbow Six Siege",
    "R6S": "Rainbow Six Siege",
    # Rocket League
    "RL": "Rocket League",
    # Street Fighter
    "SF6": "Street Fighter 6",
    "Street Fighter": "Street Fighter 6",
    # Tekken
    "Tekken": "Tekken 8",
    "T8": "Tekken 8",
    # Mobile Legends
    "MLBB": "Mobile Legends: Bang Bang",
    "Mobile Legends": "Mobile Legends: Bang Bang",
    "Mobile Legends Bang Bang": "Mobile Legends: Bang Bang",
    # Other directly accepted strings
    "FIFA Bots": "FIFA Bots",
    "NBA 2K": "NBA2K",
    "King of Glory": "King of Glory",
    "Crossfire": "Crossfire",
    "eCricket": "eCricket",
    "ECricket": "eCricket",
}


def normalise_game(game_raw: str) -> str:
    """
    Map game_raw to canonical game names using a hardcoded alias dict first,
    then rapidfuzz fallback. Return raw string if nothing matches close enough.
    """
    if not game_raw:
        return None

    # Check direct aliases
    for alias, canonical in KNOWN_ALIASES.items():
        if alias.lower() == game_raw.lower():
            return canonical

    # Check exact canonical names
    for canonical in CANONICAL_GAMES:
        if canonical.lower() == game_raw.lower():
            return canonical

    # Fuzzy match fallback
    result = process.extractOne(game_raw, CANONICAL_GAMES, scorer=fuzz.WRatio)
    if result:
        match, score, _ = result
        if score > 80:
            return match

    logger.warning(f"No match found for game_raw: {game_raw} (confidence <= 80), returning raw")
    return game_raw
