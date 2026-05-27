import os
import requests
import logging
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AlphaKeyManager:
    """Manages the rotation of the 21 Odds-API keys."""
    def __init__(self, keys_str):
        self.keys = [k.strip() for k in keys_str.split(',') if k.strip()]
        if not self.keys:
            raise ValueError("No API keys provided.")
        self.current_index = 0
        logger.info(f"Initialized KeyManager with {len(self.keys)} keys.")
        
    def get_key(self):
        return self.keys[self.current_index]

    def rotate(self):
        """Rotates to the next key. Used when hitting a 429 (Rate Limit)."""
        self.current_index = (self.current_index + 1) % len(self.keys)
        logger.info(f"Rotated API Key. Now using index {self.current_index}")

class TennisAlphaScraper:
    """Core logic for discovering, polling, and storing odds."""
    def __init__(self, key_manager: AlphaKeyManager, db_client: Client):
        self.km = key_manager
        self.db = db_client
        self.base_url = "https://api.the-odds-api.com/v4/sports"

    def fetch_with_rotation(self, url, params):
        """Helper to fetch data and rotate key on rate limits or credit exhaustion."""
        for attempt in range(len(self.km.keys)):
            params['apiKey'] = self.km.get_key()
            response = requests.get(url, params=params)
            
            # 429 means we hit the rate limit for this key
            if response.status_code == 429:
                logger.warning("Hit 429 Rate Limit. Rotating Key...")
                self.km.rotate()
                continue
                
            # 401 OUT_OF_USAGE_CREDITS means the key is out of credits for the month
            if response.status_code == 401:
                try:
                    res_json = response.json()
                    if res_json.get('error_code') == 'OUT_OF_USAGE_CREDITS':
                        logger.warning(f"API key index {self.km.current_index} is out of credits. Rotating Key...")
                        self.km.rotate()
                        continue
                except Exception:
                    pass
            
            if response.status_code != 200:
                logger.error(f"Failed to fetch data: {response.status_code} - {response.text}")
                return None
                
            remaining = response.headers.get('x-requests-remaining')
            logger.info(f"Quota Remaining for current key (index {self.km.current_index}): {remaining}")
            return response.json()
            
        logger.error("All configured API keys are exhausted or invalid.")
        return None

    def get_active_tennis_tournaments(self):
        """Finds all active 'tennis_' keys (Cost: 0 Credits)"""
        url = f"{self.base_url}"
        data = self.fetch_with_rotation(url, {})
        if not data: return []
        tennis_keys = [s['key'] for s in data if s['group'] == 'Tennis']
        return tennis_keys

    def process_odds_data(self, sport_key, match_id):
        """Fetches H2H odds and Betfair Liquidity (Cost: 1 Credit) and saves to DB."""
        url = f"{self.base_url}/{sport_key}/odds"
        params = {
            'eventIds': match_id, # Target specific match to save bandwidth
            'regions': 'eu',
            'markets': 'h2h',
            'oddsFormat': 'decimal',
            'includeBetLimits': 'true'
        }
        data = self.fetch_with_rotation(url, params)
        if not data: return
        
        match = data[0] # Since we pass eventIds, we get 1 match back
        snapshots = []
        now = datetime.now(timezone.utc).isoformat()
        
        for bookmaker in match.get('bookmakers', []):
            snapshot = {
                'match_id': match['id'],
                'scraped_at': now,
                'bookmaker': bookmaker['key'],
                'market_type': 'h2h',
                'last_update': bookmaker['last_update']
            }
            
            for market in bookmaker.get('markets', []):
                if market['key'] == 'h2h':
                    outcomes = market['outcomes']
                    # Assuming outcomes[0] is home, outcomes[1] is away based on typical API structure
                    # We should be safe if we rely on the match_registry names later
                    snapshot['home_price'] = outcomes[0]['price'] if len(outcomes) > 0 else None
                    snapshot['away_price'] = outcomes[1]['price'] if len(outcomes) > 1 else None
                    
                    # Capture liquidity (bet_limit) from exchanges like Betfair
                    snapshot['home_liquidity'] = outcomes[0].get('bet_limit') if len(outcomes) > 0 else None
                    snapshot['away_liquidity'] = outcomes[1].get('bet_limit') if len(outcomes) > 1 else None
            
            snapshots.append(snapshot)
            
        if snapshots:
            # Insert into Supabase
            self.db.table('odds_snapshots').insert(snapshots).execute()
            logger.info(f"Saved {len(snapshots)} bookmaker snapshots for match {match_id}")

    def run_alpha_pipeline(self):
        """Main execution flow implementing the Alpha-Zone Logic."""
        logger.info("Starting Alpha-Zone Polling Pipeline...")
        now = datetime.now(timezone.utc)
        
        active_tours = self.get_active_tennis_tournaments()
        
        for sport_key in active_tours:
            # 1. Fetch Schedule (Cost 0)
            url = f"{self.base_url}/{sport_key}/events"
            events = self.fetch_with_rotation(url, {})
            if not events: continue
            
            for event in events:
                commence_time = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
                time_until_start = commence_time - now
                
                # Register Match if new (The Anchor)
                # We use UPSERT in case we already have it
                registry_data = {
                    'api_event_id': event['id'],
                    'sport_key': sport_key,
                    'commence_time': event['commence_time'],
                    'home_player': event['home_team'],
                    'away_player': event['away_team']
                }
                
                # Check if match exists in DB to determine if we need the 'Opening Line'
                existing_match = self.db.table('match_registry').select('api_event_id').eq('api_event_id', event['id']).execute()
                is_new_match = len(existing_match.data) == 0
                
                if is_new_match:
                    self.db.table('match_registry').upsert(registry_data).execute()
                    logger.info(f"New Match Discovered: {event['home_team']} vs {event['away_team']}. Fetching Opening Line.")
                    self.process_odds_data(sport_key, event['id'])
                    continue # Already polled, move to next
                
                # --- The Lean Alpha Zone Logic (Targeting 10,500 Credits/Month) ---
                hours_until_start = time_until_start.total_seconds() / 3600
                
                if hours_until_start < 0:
                    continue # Match already started or finished, skip odds polling
                    
                elif hours_until_start <= 2:
                    # T-minus 0-2 Hours: 30-Minute Polling (Action runs every 15, so we skip every other)
                    last_snapshot = self.db.table('odds_snapshots').select('scraped_at').eq('match_id', event['id']).order('scraped_at', desc=True).limit(1).execute()
                    should_poll = True
                    if last_snapshot.data:
                        last_scraped = datetime.fromisoformat(last_snapshot.data[0]['scraped_at'].replace('Z', '+00:00'))
                        if (now - last_scraped).total_seconds() < 1700: # ~28-30 mins buffer
                            should_poll = False
                    
                    if should_poll:
                        logger.info(f"Alpha Zone (T-{hours_until_start:.1f}h): Fetching Closing Line data for {event['id']}")
                        self.process_odds_data(sport_key, event['id'])
                    
                elif hours_until_start <= 12:
                    # T-minus 2-12 Hours: 5-Hour Broad Trend Polling
                    last_snapshot = self.db.table('odds_snapshots').select('scraped_at').eq('match_id', event['id']).order('scraped_at', desc=True).limit(1).execute()
                    should_poll = True
                    if last_snapshot.data:
                        last_scraped = datetime.fromisoformat(last_snapshot.data[0]['scraped_at'].replace('Z', '+00:00'))
                        if (now - last_scraped).total_seconds() <= 18000: # Less than or equal to 5 hours
                            should_poll = False
                            
                    if should_poll:
                        logger.info(f"Broad Trend Zone (T-{hours_until_start:.1f}h): Updating odds for {event['id']}")
                        self.process_odds_data(sport_key, event['id'])

    def run_result_backfiller(self):
        """
        Queries Supabase for matches that have started but have no winner.
        Calls the Odds API /scores endpoint to backfill the results.
        """
        logger.info("Starting Result Backfiller...")
        now = datetime.now(timezone.utc)
        
        # 1. Get all matches in the past that don't have a winner yet
        response = self.db.table('match_registry') \
            .select('api_event_id, sport_key, home_player, away_player') \
            .lt('commence_time', now.isoformat()) \
            .is_('winner_name', 'null') \
            .execute()
            
        pending_matches = response.data
        if not pending_matches:
            logger.info("No pending matches to backfill.")
            return
            
        logger.info(f"Found {len(pending_matches)} matches awaiting results.")
        
        # Group by sport_key to minimize API calls (we fetch scores per sport)
        sport_keys = set(m['sport_key'] for m in pending_matches)
        
        for sport_key in sport_keys:
            logger.info(f"Fetching scores for {sport_key}...")
            url = f"{self.base_url}/{sport_key}/scores"
            params = {'daysFrom': 3} # Max 3 days for free tier
            
            data = self.fetch_with_rotation(url, params)
            if not data: continue
            
            # Create a lookup dictionary from the API response
            api_scores = {event['id']: event for event in data if event.get('completed')}
            
            updates_made = 0
            for match in pending_matches:
                if match['sport_key'] != sport_key: continue
                
                event_id = match['api_event_id']
                if event_id in api_scores:
                    api_event = api_scores[event_id]
                    scores = api_event.get('scores')
                    
                    if scores and len(scores) == 2:
                        # Extract scores
                        try:
                            s1 = int(scores[0]['score'] or 0)
                            s2 = int(scores[1]['score'] or 0)
                            name1 = scores[0]['name']
                            name2 = scores[1]['name']
                            
                            if s1 > s2:
                                winner = name1
                                loser = name2
                            elif s2 > s1:
                                winner = name2
                                loser = name1
                            else:
                                continue # Draw/Invalid for tennis
                                
                            score_string = f"{name1} {s1} - {s2} {name2}"
                            
                            # Update Supabase
                            self.db.table('match_registry').update({
                                'winner_name': winner,
                                'loser_name': loser,
                                'final_score_string': score_string
                            }).eq('api_event_id', event_id).execute()
                            
                            updates_made += 1
                            logger.info(f"Backfilled: {winner} def {loser}")
                            
                        except Exception as e:
                            logger.error(f"Error parsing score for {event_id}: {e}")
                            
            logger.info(f"Completed backfill for {sport_key}. Updated {updates_made} matches.")

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    
    keys_env = os.getenv("ODDS_API_KEYS")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    
    if keys_env and supabase_url and supabase_key:
        db = create_client(supabase_url, supabase_key)
        km = AlphaKeyManager(keys_env)
        scraper = TennisAlphaScraper(km, db)
        
        mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
        
        if mode == "backfill":
            scraper.run_result_backfiller()
        else:
            scraper.run_alpha_pipeline()
    else:
        logger.warning("Environment variables missing. Setup secrets to run.")
