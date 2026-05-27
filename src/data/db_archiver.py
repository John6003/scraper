import os
import sys
import csv
import psycopg2
import requests
import zipfile
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Build postgres connection string from Supabase credentials if raw pg url is not provided
CONN_STR = os.getenv("SUPABASE_PG_URL") 
if not CONN_STR:
    CONN_STR = "postgresql://postgres:vaisakh670595@db.ctkdfxcsjsuzthjfjqre.supabase.co:5432/postgres"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SIZE_THRESHOLD_MB = 400
SIZE_THRESHOLD_BYTES = SIZE_THRESHOLD_MB * 1024 * 1024

def check_db_size(cursor):
    cursor.execute("SELECT pg_total_relation_size('odds_snapshots');")
    size_bytes = cursor.fetchone()[0]
    return size_bytes

def run_archive():
    print("Connecting to DB...")
    try:
        conn = psycopg2.connect(CONN_STR)
        cursor = conn.cursor()
    except Exception as e:
        print(f"Failed to connect to DB: {e}")
        sys.exit(1)
        
    size = check_db_size(cursor)
    print(f"Current odds_snapshots size: {size / (1024*1024):.2f} MB")
    
    if size < SIZE_THRESHOLD_BYTES:
        print(f"Size is below {SIZE_THRESHOLD_MB} MB limit. No archiving needed.")
        return
        
    print(f"Size threshold exceeded! Archiving older data...")
    
    print("Fetching oldest 500,000 records...")
    cursor.execute("SELECT * FROM odds_snapshots ORDER BY scraped_at ASC LIMIT 500000;")
    rows = cursor.fetchall()
    
    if not rows:
        print("No records found.")
        return
        
    colnames = [desc[0] for desc in cursor.description]
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"odds_archive_{timestamp}.csv"
    zip_filename = f"odds_archive_{timestamp}.zip"
    
    print(f"Writing database rows to {csv_filename}...")
    with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(colnames)
        writer.writerows(rows)
        
    print(f"Compressing to lossless ZIP: {zip_filename}...")
    try:
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(csv_filename, arcname=csv_filename)
        print("Compression complete!")
    except Exception as e:
        print(f"Compression failed: {e}")
        # Cleanup and abort to prevent data loss
        if os.path.exists(csv_filename): os.remove(csv_filename)
        return
        
    print("Sending ZIP archive to Telegram...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram credentials. Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        print("Aborting delete to prevent data loss.")
        if os.path.exists(csv_filename): os.remove(csv_filename)
        if os.path.exists(zip_filename): os.remove(zip_filename)
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(zip_filename, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': f"Automated Archive: {len(rows)} rows from odds_snapshots (Compressed ZIP)."}
            response = requests.post(url, data=data, files=files)
    except Exception as e:
        print(f"Network error sending document to Telegram: {e}")
        response = None
        
    if response and response.status_code == 200:
        print("Successfully sent to Telegram. Deleting records from database...")
        # Get the IDs of the records to delete
        snapshot_ids = [r[0] for r in rows] # Assuming snapshot_id is the first column
        # Delete in chunks to avoid locking too much
        chunk_size = 10000
        deleted_count = 0
        for i in range(0, len(snapshot_ids), chunk_size):
            chunk = snapshot_ids[i:i+chunk_size]
            cursor.execute("DELETE FROM odds_snapshots WHERE snapshot_id = ANY(%s);", (chunk,))
            conn.commit()
            deleted_count += len(chunk)
            print(f"Deleted {deleted_count} / {len(snapshot_ids)} records.")
            
        print("Archiving complete!")
    else:
        err_msg = response.text if response else "No response"
        print(f"Failed to send to Telegram: {err_msg}")
        print("Aborting delete to prevent data loss.")
        
    # Cleanup local files
    if os.path.exists(csv_filename):
        os.remove(csv_filename)
    if os.path.exists(zip_filename):
        os.remove(zip_filename)
        
    cursor.close()
    conn.close()

if __name__ == "__main__":
    run_archive()
