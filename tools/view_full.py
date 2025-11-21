import sqlite3
import pandas as pd

DB_PATH = 'geolocation.db'
conn = sqlite3.connect(DB_PATH)

print("\n" + "="*80)
print("STATISTIQUES GÉNÉRALES")
print("="*80)

df = pd.read_sql_query("""
    SELECT room, floor, COUNT(*) as samples, COUNT(DISTINCT mac) as unique_aps
    FROM fingerprints
    GROUP BY room, floor
""", conn)
print(df.to_string(index=False))

print("\n" + "="*80)
print("TOP 10 MACs LES PLUS DÉTECTÉES")
print("="*80)

df = pd.read_sql_query("""
    SELECT mac, COUNT(*) as count, ROUND(AVG(rssi), 1) as avg_rssi
    FROM fingerprints
    GROUP BY mac
    ORDER BY count DESC
    LIMIT 10
""", conn)
print(df.to_string(index=False))

print("\n" + "="*80)
print("DERNIÈRES 20 DÉTECTIONS")
print("="*80)

df = pd.read_sql_query("""
    SELECT room, floor, mac, rssi, timestamp
    FROM fingerprints
    ORDER BY timestamp DESC
    LIMIT 20
""", conn)
print(df.to_string(index=False))

print("\n" + "="*80)
print("RSSI MOYEN PAR SALLE ET PAR MAC")
print("="*80)

df = pd.read_sql_query("""
    SELECT room, mac, COUNT(*) as detections, 
           ROUND(AVG(rssi), 1) as avg_rssi,
           MIN(rssi) as min_rssi,
           MAX(rssi) as max_rssi
    FROM fingerprints
    GROUP BY room, mac
    ORDER BY room, avg_rssi DESC
""", conn)
print(df.to_string(index=False))

print("\n" + "="*80)
print("COORDONNÉES GPS PAR SALLE")
print("="*80)

df = pd.read_sql_query("""
    SELECT room, floor, 
           ROUND(AVG(lat), 6) as avg_lat, 
           ROUND(AVG(lon), 6) as avg_lon,
           COUNT(DISTINCT timestamp) as samples
    FROM fingerprints
    GROUP BY room, floor
""", conn)
print(df.to_string(index=False))

conn.close()

print("\n✓ Analyse terminée\n")