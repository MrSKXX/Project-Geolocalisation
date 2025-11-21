import paho.mqtt.client as mqtt
import json
import base64
import sqlite3
from datetime import datetime

DB_PATH = 'geolocation.db'

current_room = None
current_floor = None
current_location = None
current_lat = None
current_lon = None
sample_count = 0
target_samples = 5

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS fingerprints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT,
        floor TEXT,
        location TEXT,
        lat REAL,
        lon REAL,
        mac TEXT,
        ssid TEXT,
        rssi INTEGER,
        timestamp TEXT
    )''')
    conn.commit()
    conn.close()

def decode_payload(b64_payload):
    buf = base64.b64decode(b64_payload)
    num_aps = len(buf) // 7
    aps = []
    
    for i in range(num_aps):
        offset = i * 7
        mac_bytes = buf[offset:offset+6]
        rssi_byte = buf[offset+6]
        rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
        mac = ':'.join(f'{b:02x}' for b in mac_bytes)
        
        if mac != '00:00:00:00:00:00':
            aps.append({'mac': mac, 'rssi': rssi})
    
    return aps

def save_fingerprints(aps):
    global sample_count
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    timestamp = datetime.now().isoformat()
    
    for ap in aps:
        c.execute('''INSERT INTO fingerprints 
                     (room, floor, location, lat, lon, mac, ssid, rssi, timestamp)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (current_room, current_floor, current_location, 
                   current_lat, current_lon, ap['mac'], 'Unknown', ap['rssi'], timestamp))
    
    conn.commit()
    conn.close()
    
    sample_count += 1
    print(f"✓ Échantillon {sample_count}/{target_samples} enregistré ({len(aps)} APs)")
    
    if sample_count >= target_samples:
        print(f"\n🎉 {target_samples} échantillons collectés pour salle {current_room} !")
        print("Tapez 'next' pour changer de position ou 'quit' pour arrêter\n")

def on_message(client, userdata, msg):
    if current_room is None:
        return
    
    try:
        payload = json.loads(msg.payload.decode())
        
        if 'uplink_message' not in payload:
            return
        
        b64_data = payload['uplink_message']['frm_payload']
        aps = decode_payload(b64_data)
        
        if aps:
            save_fingerprints(aps)
        
    except Exception as e:
        print(f"✗ Erreur: {e}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✓ Connecté à TTN\n")
        client.subscribe("v3/project1-sniffer@ttn/devices/esp32-lora-sniffer/up")
    else:
        print(f"✗ Connexion échouée: {rc}")

def set_location():
    global current_room, current_floor, current_location, current_lat, current_lon, sample_count
    
    print("\n" + "="*60)
    print("NOUVELLE POSITION - SALLE 203")
    print("="*60)
    
    current_room = input("Salle (ex: 203): ").strip()
    current_floor = input("Étage (ex: 2): ").strip()
    current_location = f"Salle {current_room}"
    
    print("\n📍 Ouvre Google Maps et trouve la position exacte")
    print("   Clic droit sur la carte → Copie les coordonnées\n")
    
    lat_str = input("Latitude: ").strip()
    lon_str = input("Longitude: ").strip()
    
    try:
        current_lat = float(lat_str)
        current_lon = float(lon_str)
        print(f"✓ Coordonnées validées: {current_lat}, {current_lon}")
    except:
        print("✗ Coordonnées invalides !")
        return False
    
    sample_count = 0
    
    print(f"\n📍 Configuration OK")
    print(f"   Salle: {current_room} | Étage: {current_floor}")
    print(f"   Attente de {target_samples} échantillons (~5 minutes)...\n")
    
    return True

if __name__ == '__main__':
    print("""
╔═══════════════════════════════════════════════════════════╗
║     COLLECTE AUTOMATIQUE - SALLE 203                      ║
║     5 échantillons = ~5 minutes                          ║
╚═══════════════════════════════════════════════════════════╝
""")
    
    init_db()
    
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except:
        client = mqtt.Client()
    
    client.username_pw_set(
        "project1-sniffer@ttn",
        "NNSXS.URZ75UUXP7WCFQJ33P4XTXTL4D4YXK2D2A5P63A.AAKKN5KZOCIFHZ6KA654WBQXXYUOTKUONITP5DEJKMAP2EONXMRQ"
    )
    
    client.on_connect = on_connect
    client.on_message = on_message
    
    client.connect("eu1.cloud.thethings.network", 1883, 60)
    client.loop_start()
    
    if not set_location():
        print("Arrêt - coordonnées invalides")
        exit()
    
    try:
        while True:
            cmd = input("Commande (next/quit): ").strip().lower()
            
            if cmd == 'quit':
                break
            elif cmd == 'next':
                if not set_location():
                    break
    
    except KeyboardInterrupt:
        print("\n\nArrêt...")
    
    client.loop_stop()
    client.disconnect()
    
    print("\n✓ Collecte terminée")
    
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    rooms = conn.execute("SELECT DISTINCT room FROM fingerprints").fetchall()
    conn.close()
    
    print(f"✓ Total: {total} fingerprints")
    print(f"✓ Salles: {', '.join([r[0] for r in rooms])}")