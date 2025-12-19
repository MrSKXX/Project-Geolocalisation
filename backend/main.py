from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
from typing import Dict, List, Optional
from datetime import datetime
from collections import defaultdict
import asyncio
import os

# ================================================================
# CONFIGURATION
# ================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'geolocation.db')
PHOTOS_DIR = os.path.join(BASE_DIR, 'Photos')
FRONTEND_DIR = os.path.join(BASE_DIR, '../frontend')

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists(PHOTOS_DIR):
    os.makedirs(PHOTOS_DIR)

app.mount("/photos", StaticFiles(directory=PHOTOS_DIR), name="photos")

# ================================================================
# VARIABLES GLOBALES
# ================================================================

ap_database: Dict = {}
fingerprint_data = []
current_position = None
websocket_connections: List[WebSocket] = []

ROOM_PHOTOS = {
    '201': '201.jpeg',
    '203': '203.jpeg',
    '206': '206.jpeg',
    '305': '305.jpeg',
    'Atelier M.Viateur': 'Atelier M.Viateur.jpeg'
}

FLOOR_MAPS = {
    '2': 'Etage2.jpeg',
    '3': 'plan-etage3.jpeg'
}

# ================================================================
# MODÈLES PYDANTIC
# ================================================================

class WifiNetwork(BaseModel):
    ssid: str
    mac: str
    rssi: int
    channel: Optional[int] = 0

class ScanPayload(BaseModel):
    scanner_id: str
    networks: List[WifiNetwork]

class CollectPointRequest(BaseModel):
    location_name: str
    description: Optional[str] = ""
    lat: float
    lon: float
    accuracy: Optional[float] = 0.0
    networks: List[WifiNetwork]

# ================================================================
# BASE DE DONNÉES
# ================================================================

def init_db():
    """Créer la table si elle n'existe pas"""
    try:
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
        print("✓ Table fingerprints initialisée")
    except Exception as e:
        print(f"✗ Erreur init DB: {e}")

def load_database():
    """Charger les empreintes WiFi"""
    global ap_database, fingerprint_data
    
    if not os.path.exists(DB_PATH):
        print(f"⚠️ Base vide à {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM fingerprints")
        rows = c.fetchall()
        
        fingerprint_data = []
        for row in rows:
            fingerprint_data.append({
                'room': row[1], 'floor': row[2], 'location': row[3],
                'lat': row[4], 'lon': row[5], 'mac': row[6].lower(),
                'ssid': row[7], 'rssi': row[8]
            })
        
        zones_data = defaultdict(lambda: {
            'macs': set(), 'lat': [], 'lon': [], 
            'location': '', 'floor': '', 'room': ''
        })
        
        for fp in fingerprint_data:
            lat_key = round(fp['lat'] * 10000) / 10000
            lon_key = round(fp['lon'] * 10000) / 10000
            key = f"{lat_key}_{lon_key}"
            
            zones_data[key]['macs'].add(fp['mac'])
            zones_data[key]['lat'].append(fp['lat'])
            zones_data[key]['lon'].append(fp['lon'])
            zones_data[key]['location'] = fp['location']
            zones_data[key]['floor'] = fp['floor']
            zones_data[key]['room'] = fp['room']
        
        ap_database = {}
        for zone_key, data in zones_data.items():
            if len(data['lat']) > 0:
                avg_lat = sum(data['lat']) / len(data['lat'])
                avg_lon = sum(data['lon']) / len(data['lon'])
                for mac in data['macs']:
                    if mac not in ap_database:
                        ap_database[mac] = {
                            'ssid': 'Unknown', 'lat': avg_lat, 'lon': avg_lon,
                            'location': data['location'], 
                            'floor': data['floor'], 
                            'room': data['room']
                        }
        
        conn.close()
        print(f"✓ Base chargée: {len(ap_database)} APs / {len(fingerprint_data)} empreintes")
    except Exception as e:
        print(f"✗ Erreur chargement: {e}")

# ================================================================
# ALGORITHME DE LOCALISATION
# ================================================================

def advanced_rssi_matching(aps: List[Dict]) -> Optional[Dict]:
    """Fingerprinting RSSI"""
    if not fingerprint_data: return None
    
    best_match = None
    best_score = -float('inf')
    
    zones_data = defaultdict(lambda: {
        'rssi_by_mac': defaultdict(list), 'lat': [], 'lon': [], 
        'location': '', 'floor': '', 'room': '', 'all_macs': set()
    })
    
    for fp in fingerprint_data:
        lat_key = round(fp['lat'] * 10000) / 10000
        lon_key = round(fp['lon'] * 10000) / 10000
        key = f"{lat_key}_{lon_key}"
        
        zones_data[key]['rssi_by_mac'][fp['mac']].append(fp['rssi'])
        zones_data[key]['lat'].append(fp['lat'])
        zones_data[key]['lon'].append(fp['lon'])
        zones_data[key]['location'] = fp['location']
        zones_data[key]['room'] = fp['room']
        zones_data[key]['floor'] = fp['floor']
        zones_data[key]['all_macs'].add(fp['mac'])
    
    detected_macs = {ap['mac'].lower(): ap['rssi'] for ap in aps}
    
    for zone_key, data in zones_data.items():
        score = 0
        weight_sum = 0
        matched_count = 0
        
        for mac, rssi_list in data['rssi_by_mac'].items():
            avg_known = sum(rssi_list) / len(rssi_list)
            
            if mac in detected_macs:
                detected = detected_macs[mac]
                diff = abs(detected - avg_known)
                weight = 1.0 / (1.0 + diff/10.0) 
                score += (100 - min(diff, 100)) * weight
                weight_sum += weight
                matched_count += 1
            else:
                score -= 5
        
        if weight_sum > 0 and matched_count >= 2:
            final_score = score / weight_sum
            coverage = matched_count / len(data['all_macs']) if data['all_macs'] else 0
            final_score *= (0.5 + 0.5 * coverage)
            
            if final_score > best_score:
                best_score = final_score
                best_match = {
                    'room': data['room'],
                    'floor': data['floor'],
                    'location': data['location'],
                    'lat': sum(data['lat']) / len(data['lat']),
                    'lon': sum(data['lon']) / len(data['lon']),
                    'confidence': min(max(final_score / 100, 0), 1.0),
                    'matched_aps': matched_count
                }

    return best_match

def locate_position(aps: List[Dict]) -> Dict:
    """Localisation principale"""
    if not aps: 
        return {'success': False, 'error': 'Scan vide', 'type': 'position_update'}
    
    match = advanced_rssi_matching(aps)
    
    result = {
        'timestamp': datetime.now().isoformat(), 
        'success': False,
        'type': 'position_update'
    }

    if match and match['confidence'] > 0.2:
        result.update({
            'success': True,
            'method': 'Fingerprinting',
            'lat': match['lat'], 'lon': match['lon'],
            'room': match['room'], 'floor': match['floor'],
            'location': match['location'],
            'confidence': f"{match['confidence']*100:.0f}%",
            'matched_aps': match['matched_aps'],
            'room_photo': ROOM_PHOTOS.get(match['room']),
            'floor_map': FLOOR_MAPS.get(match['floor'])
        })
    else:
        result['error'] = 'Position inconnue'
        
    return result

# ================================================================
# WEBSOCKET
# ================================================================

async def broadcast_to_websockets(data: Dict):
    """Broadcast à tous les clients"""
    disconnected = []
    for ws in websocket_connections:
        try:
            await ws.send_json(data)
        except:
            disconnected.append(ws)
    
    for ws in disconnected:
        if ws in websocket_connections:
            websocket_connections.remove(ws)

# ================================================================
# ROUTES HTTP
# ================================================================

@app.post("/api/update-http")
async def receive_http_scan(payload: ScanPayload):
    """Réception scan ESP32 via HTTP"""
    global current_position
    
    aps = [{'mac': net.mac, 'rssi': net.rssi, 'ssid': net.ssid} for net in payload.networks]
    
    print(f"\n📡 [HTTP] Scanner: {payload.scanner_id}")
    print(f"   APs: {len(aps)}")
    
    # Broadcast scan brut (pour collecte)
    raw_scan_msg = {
        'type': 'raw_scan',
        'scanner_id': payload.scanner_id,
        'networks': [
            {'ssid': n.ssid, 'mac': n.mac, 'rssi': n.rssi, 'channel': n.channel or 0} 
            for n in payload.networks
        ]
    }
    
    await broadcast_to_websockets(raw_scan_msg)
    
    # Calcul position
    result = locate_position(aps)
    current_position = result
    
    if result['success']:
        print(f"📍 [LOC] {result['location']} ({result['confidence']})")
    
    await broadcast_to_websockets(result)
    
    return {"status": "success", "processed": len(aps)}

@app.post("/api/collect-point")
async def collect_point(req: CollectPointRequest):
    """Enregistrer un point de collecte"""
    print(f"\n💾 [COLLECT] {req.location_name}")
    print(f"   GPS: {req.lat}, {req.lon}")
    print(f"   APs: {len(req.networks)}")
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        timestamp = datetime.now().isoformat()
        count = 0
        
        for net in req.networks:
            try:
                c.execute('''INSERT INTO fingerprints 
                             (room, floor, location, lat, lon, mac, ssid, rssi, timestamp)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                          ("CAMPUS", "3", req.location_name, req.lat, req.lon,
                           net.mac.lower(), net.ssid, net.rssi, timestamp))
                count += 1
            except Exception as e:
                print(f"   ⚠️ Erreur réseau {net.mac}: {e}")
        
        conn.commit()
        conn.close()
        
        if count == 0:
            return {"success": False, "message": "Aucun réseau enregistré"}
        
        load_database()
        
        print(f"✓ '{req.location_name}' → {count} APs")
        
        return {
            "success": True, 
            "message": f"Point '{req.location_name}' enregistré",
            "aps_saved": count
        }
        
    except Exception as e:
        print(f"❌ Erreur: {e}")
        return {"success": False, "message": f"Erreur: {e}"}

@app.get("/api/collected-points")
async def get_collected_points():
    """Liste des points collectés"""
    try:
        if not os.path.exists(DB_PATH): 
            return {"points": []}
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT location, AVG(lat), AVG(lon), COUNT(*) 
            FROM fingerprints 
            GROUP BY location
        """)
        rows = c.fetchall()
        conn.close()
        
        return {
            "points": [
                {"location": r[0], "lat": r[1], "lon": r[2], "samples": r[3]} 
                for r in rows
            ]
        }
    except Exception as e:
        return {"points": [], "error": str(e)}

@app.get("/api/position")
async def get_pos():
    """Position actuelle"""
    return current_position if current_position else {"success": False}

@app.get("/")
async def root():
    """Page principale (carte OpenStreetMap)"""
    index = os.path.join(FRONTEND_DIR, 'index.html')
    if os.path.exists(index): 
        return FileResponse(index)
    return {"error": "index.html introuvable"}

@app.get("/collect")
async def collect_page():
    """Page de collecte"""
    collect = os.path.join(FRONTEND_DIR, 'collect.html')
    if os.path.exists(collect): 
        return FileResponse(collect)
    return {"error": "collect.html introuvable"}

@app.get("/indoor")
async def indoor_page():
    """Page indoor avec plan du bâtiment"""
    indoor = os.path.join(FRONTEND_DIR, 'indoor.html')
    if os.path.exists(indoor):
        return FileResponse(indoor)
    return {"error": "indoor.html introuvable"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket pour communication temps réel"""
    await websocket.accept()
    websocket_connections.append(websocket)
    print(f"✓ WebSocket connecté ({len(websocket_connections)} clients)")
    
    try:
        while True: 
            await websocket.receive_text()
    except:
        pass
    finally:
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)
        print(f"✗ WebSocket déconnecté ({len(websocket_connections)} clients)")

# ================================================================
# STARTUP
# ================================================================

@app.on_event("startup")
async def startup():
    """Initialisation au démarrage"""
    print("\n" + "="*60)
    print("🚀 SERVEUR GEOLOCALISATION WiFi")
    print("="*60)
    
    init_db()
    load_database()
    
    print("\n✓ Serveur prêt sur http://0.0.0.0:8000")
    print("  • Carte:    http://localhost:8000")
    print("  • Indoor:   http://localhost:8000/indoor")
    print("  • Collecte: http://localhost:8000/collect")
    print("="*60 + "\n")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)