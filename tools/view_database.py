import sqlite3
import pandas as pd

DB_PATH = 'geolocation.db'

conn = sqlite3.connect(DB_PATH)

print("\n=== FINGERPRINTS PAR SALLE ===")
df = pd.read_sql_query("""
    SELECT room, floor, COUNT(*) as samples,
           COUNT(DISTINCT mac) as unique_aps
    FROM fingerprints
    GROUP BY room, floor
""", conn)
print(df)

print("\n=== TOP APs ===")
df = pd.read_sql_query("""
    SELECT mac, COUNT(*) as count, ROUND(AVG(rssi), 1) as avg_rssi
    FROM fingerprints
    GROUP BY mac
    ORDER BY count DESC
    LIMIT 5
""", conn)
print(df)

conn.close()