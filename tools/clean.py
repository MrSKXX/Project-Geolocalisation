import sqlite3

DB_PATH = 'geolocation.db'

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=== VÉRIFICATION COMPLÈTE ===\n")

# Vérifier tous les types de NULL
c.execute("SELECT COUNT(*) FROM fingerprints WHERE floor IS NULL")
print(f"❌ floor IS NULL: {c.fetchone()[0]}")

c.execute("SELECT COUNT(*) FROM fingerprints WHERE lat IS NULL")
print(f"❌ lat IS NULL: {c.fetchone()[0]}")

c.execute("SELECT COUNT(*) FROM fingerprints WHERE lon IS NULL")
print(f"❌ lon IS NULL: {c.fetchone()[0]}")

c.execute("SELECT COUNT(*) FROM fingerprints WHERE rssi IS NULL")
print(f"❌ rssi IS NULL: {c.fetchone()[0]}")

# Afficher quelques lignes problématiques
print("\n=== LIGNES AVEC DES NULL ===")
c.execute("SELECT id, room, floor, lat, lon, rssi FROM fingerprints WHERE floor IS NULL OR lat IS NULL OR lon IS NULL LIMIT 10")
rows = c.fetchall()
if rows:
    for row in rows:
        print(f"ID {row[0]}: room={row[1]}, floor={row[2]}, lat={row[3]}, lon={row[4]}, rssi={row[5]}")
else:
    print("✅ Aucune ligne avec NULL trouvée")

# Supprimer TOUTES les lignes avec ANY NULL
print("\n🗑️  SUPPRESSION DE TOUTES LES LIGNES AVEC NULL...")
c.execute("DELETE FROM fingerprints WHERE floor IS NULL OR lat IS NULL OR lon IS NULL OR rssi IS NULL")
deleted = c.rowcount
conn.commit()
print(f"✓ {deleted} lignes supprimées")

print("\n=== VÉRIFICATION FINALE ===")
total = c.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
print(f"✅ Total fingerprints restants: {total}")

c.execute("SELECT room, floor, COUNT(*) FROM fingerprints GROUP BY room, floor ORDER BY floor, room")
print("\n📋 Salles restantes:")
for row in c.fetchall():
    print(f"  {row[0]:25s} | Étage {row[1]:4s} | {row[2]:3d} échantillons")

conn.close()