import os
import requests
import logging
from datetime import datetime, timezone
from supabase import create_client
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class LiveFlashscoreSync:
    def __init__(self):
        load_dotenv()
        self.db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "x-fsign": "SW9D1eZo"
        }
        self.SEP = chr(172)
        self.VAL = chr(247)

    def parse_fs(self, fs_str):
        if not fs_str or fs_str == '0': return {}
        data = {}
        for part in fs_str.replace('~', self.SEP).split(self.SEP):
            if self.VAL in part:
                kv = part.split(self.VAL, 1)
                data[kv[0]] = kv[1]
        return data

    def robust_match_names(self, name1, name2):
        import unicodedata
        def get_tokens(n):
            n = unicodedata.normalize('NFKD', str(n)).encode('ascii', 'ignore').decode('ascii')
            n = n.replace('.', '').replace(',', '').replace('-', ' ').lower().strip()
            return [p for p in n.split(' ') if len(p) > 1]
        
        t1 = set(get_tokens(name1))
        t2 = set(get_tokens(name2))
        
        if not t1 or not t2: return False
        common = t1.intersection(t2)
        if common and any(len(c) > 2 for c in common):
            return True
        return False

    def fetch_flashscore_day(self, days_ago):
        """Fetches the global tennis overview feed for a specific day offset."""
        url = f"https://global.flashscore.ninja/2/x/feed/f_2_{-days_ago}_5_en_1"
        logger.info(f"Sweeping Ninja Feed: {url}")
        
        r = requests.get(url, headers=self.headers)
        if r.status_code != 200:
            logger.error(f"Failed to fetch {url}")
            return []
            
        import re
        # Use the battle-tested regex from master_harvester
        # ~AA÷ID
        match_ids = re.findall(r"~AA" + self.VAL + r"([A-Za-z0-9]{8})", r.text)
        logger.info(f"Found {len(match_ids)} match blocks via regex.")
        
        # Split the whole feed by match blocks
        blocks = r.text.split('~AA')[1:]
        matches = []
        
        for block in blocks:
            # The Match ID is 8 characters long, but preceded by the VAL separator (chr(247))
            mid = block[1:9]
            if mid not in match_ids: continue
            
            # Use the established player boundary logic
            # In the Ninja feed, sections start with SEP (172) followed by the key
            p1_marker = self.SEP + 'AE'
            p2_marker = self.SEP + 'AF'
            
            p1_start = block.find(p1_marker)
            p2_start = block.find(p2_marker)
            
            if p1_start == -1 or p2_start == -1: continue
            
            p1_part = block[p1_start:p2_start]
            p2_part = block[p2_start:]
            
            # Use the project-standard parse_fs
            p1_data = self.parse_fs(p1_part)
            p2_data = self.parse_fs(p2_part)
            
            # Use the project-standard General parsing
            general_data = self.parse_fs(block[:p1_start])
            
            # Determine winner via AS/AZ fields as established in v2_harvester
            p1_won = "AS" in p1_data or "AZ" in p1_data
            p2_won = "AS" in p2_data or "AZ" in p2_data
            
            # Fallback
            if not p1_won and not p2_won:
                s1_val = int(general_data.get("AG") or 0)
                s2_val = int(general_data.get("AH") or 0)
                p1_won = s1_val > s2_val
                p2_won = s2_val > s1_val

            matches.append({
                "match_id": mid,
                "p1": p1_data.get("AE"),
                "p2": p2_data.get("AF"),
                "p1_won": p1_won,
                "p2_won": p2_won,
                "s1": general_data.get("AG") or p1_data.get("AG") or 0,
                "s2": general_data.get("AH") or p2_data.get("AH") or 0,
                "status": general_data.get("AC"),
                "notes": general_data.get("AM"),
            })
            
        logger.info(f"Successfully parsed {len(matches)} matches.")
        return matches
        logger.info(f"Extracted {len(matches)} global matches for offset {-days_ago}")
        return matches

    def run_sync(self):
        logger.info("Starting Live Flashscore Sync...")
        now = datetime.now(timezone.utc)
        
        # 1. Get started orphans
        resp = self.db.table('match_registry').select('*').lt('commence_time', now.isoformat()).is_('winner_name', 'null').execute()
        orphans = resp.data
        
        if not orphans:
            logger.info("No started orphans to sync.")
            return

        logger.info(f"Found {len(orphans)} pending matches.")
        
        # 2. Categorize by days_ago and handle ghosts
        active_orphans = []
        days_to_fetch = set()
        
        void_count = 0
        for orphan in orphans:
            commence = datetime.fromisoformat(orphan['commence_time'].replace('Z', '+00:00'))
            days_ago = (now - commence).days
            
            if days_ago > 7:
                # GHOST BUSTER LOGIC
                logger.info(f"Voiding Ghost Match (>{days_ago} days old): {orphan['home_player']} vs {orphan['away_player']}")
                self.db.table('match_registry').update({
                    "winner_name": "Void",
                    "loser_name": "Void",
                    "final_score_string": "Cancelled"
                }).eq('api_event_id', orphan['api_event_id']).execute()
                void_count += 1
            else:
                active_orphans.append(orphan)
                days_to_fetch.add(days_ago)

        if not active_orphans:
            logger.info(f"Sync complete. Voided {void_count} ghost matches. No active sweeps needed.")
            return

        # 3. Fetch only the required days
        fs_data = []
        for d in days_to_fetch:
            fs_data.extend(self.fetch_flashscore_day(d))

        # 4. Token Match and Update
        updates = 0
        for orphan in active_orphans:
            match_found = None
            for res in fs_data:
                if (self.robust_match_names(res['p1'], orphan['home_player']) and self.robust_match_names(res['p2'], orphan['away_player'])) or \
                   (self.robust_match_names(res['p1'], orphan['away_player']) and self.robust_match_names(res['p2'], orphan['home_player'])):
                    match_found = res
                    break
            
            if match_found:
                try:
                    # Link ID immediately
                    self.db.table('match_registry').update({
                        "flashscore_id": match_found['match_id']
                    }).eq('api_event_id', orphan['api_event_id']).execute()

                    # ONLY update winner/score if match is FINISHED
                    # Status 3 = Finished, 7 = Retired, 8 = Walkover, 9 = Walkover (Global Feed)
                    is_finished = match_found.get('status') in ['3', '7', '8', '9']
                    
                    # Fallback: if status is missing, check if scores are non-zero
                    if not is_finished and match_found.get('status') is None:
                        s1_val = int(match_found['s1'] or 0)
                        s2_val = int(match_found['s2'] or 0)
                        if s1_val > 0 or s2_val > 0:
                            is_finished = True

                    if is_finished:
                        if match_found['p1_won']:
                            winner, loser = match_found['p1'], match_found['p2']
                        elif match_found['p2_won']:
                            winner, loser = match_found['p2'], match_found['p1']
                        else:
                            # Final fallback
                            s1, s2 = int(match_found['s1']), int(match_found['s2'])
                            winner = match_found['p1'] if s1 > s2 else match_found['p2']
                            loser = match_found['p2'] if s1 > s2 else match_found['p1']
                        
                        score = f"{match_found['p1']} {match_found['s1']} - {match_found['s2']} {match_found['p2']}"
                        if match_found.get('status') == '9' or match_found.get('status') == '8':
                            score += " (WO)"
                        
                        logger.info(f"Synced Result: {orphan['home_player']} vs {orphan['away_player']} -> Winner: {winner}")
                        
                        self.db.table('match_registry').update({
                            "winner_name": winner,
                            "loser_name": loser,
                            "final_score_string": score
                        }).eq('api_event_id', orphan['api_event_id']).execute()
                        updates += 1
                    else:
                        logger.info(f"Linked ID (Pending): {orphan['home_player']} vs {orphan['away_player']} -> {match_found['match_id']}")

                except Exception as e:
                    logger.error(f"Error updating match {orphan['api_event_id']}: {e}")

        logger.info(f"Live Sync complete. Voided: {void_count} | Updated: {updates}/{len(active_orphans)}")

if __name__ == "__main__":
    sync = LiveFlashscoreSync()
    sync.run_sync()
