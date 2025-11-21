from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import paho.mqtt.client as mqtt
import json
import base64
import sqlite3
from typing import Dict, List
import math
from datetime import datetime
from collections import defaultdict

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = '../tools/geolocation.db'
ap_database: Dict = {}
fingerprint_data = []
current_position = None
websocket_connections: List[WebSocket] = []

def load_database():
    global ap_database, fingerprint_data
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute("SELECT * FROM fingerprints")
        for row in c.fetchall():
            fingerprint_data.append({
                'room': row[1],
                'floor': row[2],
                'location': row[3],
                'lat': row[4],
                'lon': row[5],
                'mac': row[6].lower(),
                'ssid': row[7],
                'rssi': row[8],
                'timestamp': row[9]
            })
        
        rooms_data = defaultdict(lambda: {'macs': set(), 'lat': [], 'lon': [], 'location': '', 'floor': ''})
        for fp in fingerprint_data:
            key = f"{fp['room']}_{fp['floor']}"
            rooms_data[key]['macs'].add(fp['mac'])
            rooms_data[key]['lat'].append(fp['lat'])
            rooms_data[key]['lon'].append(fp['lon'])
            rooms_data[key]['location'] = fp['location']
            rooms_data[key]['floor'] = fp['floor']
        
        for room_key, data in rooms_data.items():
            room, floor = room_key.split('_')
            avg_lat = sum(data['lat']) / len(data['lat'])
            avg_lon = sum(data['lon']) / len(data['lon'])
            
            for mac in data['macs']:
                if mac not in ap_database:
                    ap_database[mac] = {
                        'ssid': 'Unknown',
                        'lat': avg_lat,
                        'lon': avg_lon,
                        'location': data['location'],
                        'floor': floor,
                        'room': room
                    }
        
        conn.close()
        print(f"✓ Loaded {len(ap_database)} unique APs from {len(fingerprint_data)} fingerprints")
        
    except Exception as e:
        print(f"✗ Error loading database: {e}")

def rssi_to_distance(rssi: int) -> float:
    rssi_at_1m = -40
    n = 2.7
    return math.pow(10, (rssi_at_1m - rssi) / (10 * n))

def decode_payload(b64_payload: str) -> List[Dict]:
    try:
        buf = base64.b64decode(b64_payload)
        
        if len(buf) % 7 != 0:
            return []
        
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
    except Exception as e:
        print(f"✗ Decode error: {e}")
        return []

def simple_rssi_matching(aps: List[Dict]) -> Dict:
    best_match = None
    best_score = -float('inf')
    
    rooms_data = defaultdict(lambda: {'rssi_by_mac': defaultdict(list), 'lat': [], 'lon': [], 'location': '', 'floor': ''})
    
    for fp in fingerprint_data:
        key = f"{fp['room']}_{fp['floor']}"
        rooms_data[key]['rssi_by_mac'][fp['mac']].append(fp['rssi'])
        rooms_data[key]['lat'].append(fp['lat'])
        rooms_data[key]['lon'].append(fp['lon'])
        rooms_data[key]['location'] = fp['location']
        rooms_data[key]['floor'] = fp['floor']
    
    detected_macs = {ap['mac'].lower(): ap['rssi'] for ap in aps}
    
    for room_key, room_data in rooms_data.items():
        score = 0
        matches = 0
        
        for mac, rssi_list in room_data['rssi_by_mac'].items():
            if mac in detected_macs:
                avg_rssi = sum(rssi_list) / len(rssi_list)
                diff = abs(detected_macs[mac] - avg_rssi)
                score += (100 - diff)
                matches += 1
        
        if matches > 0:
            score = score / matches
            if score > best_score:
                best_score = score
                room, floor = room_key.split('_')
                best_match = {
                    'room': room,
                    'floor': floor,
                    'lat': sum(room_data['lat']) / len(room_data['lat']),
                    'lon': sum(room_data['lon']) / len(room_data['lon']),
                    'location': room_data['location'],
                    'confidence': min(score / 100, 1.0),
                    'matched_aps': matches
                }
    
    return best_match

def triangulate(aps: List[Dict]) -> Dict:
    numerateur_x = 0
    numerateur_y = 0
    denominateur = 0
    matched = 0
    matched_details = []
    
    for ap in aps:
        mac_lower = ap['mac'].lower()
        known = ap_database.get(mac_lower)
        
        if known:
            distance = rssi_to_distance(ap['rssi'])
            weight = 2 / math.pow(distance, 0.65)
            
            numerateur_x += known['lat'] * weight
            numerateur_y += known['lon'] * weight
            denominateur += weight
            matched += 1
            
            matched_details.append({
                'mac': ap['mac'],
                'ssid': known['ssid'],
                'rssi': ap['rssi'],
                'distance': f"{distance:.2f}m"
            })
    
    if denominateur > 0:
        return {
            'success': True,
            'lat': numerateur_x / denominateur,
            'lon': numerateur_y / denominateur,
            'matched_aps': matched,
            'details': matched_details
        }
    else:
        return None

def locate_room(aps: List[Dict]) -> Dict:
    best_match = None
    best_rssi = -100
    
    for ap in aps:
        mac_lower = ap['mac'].lower()
        known = ap_database.get(mac_lower)
        
        if known and ap['rssi'] > best_rssi:
            best_rssi = ap['rssi']
            best_match = known
    
    if best_match:
        confidence = 'Haute' if best_rssi > -50 else 'Moyenne' if best_rssi > -70 else 'Faible'
        return {
            'room': best_match['room'],
            'floor': best_match['floor'],
            'location': best_match['location'],
            'rssi': best_rssi,
            'confidence': confidence
        }
    else:
        return {
            'room': 'Unknown',
            'floor': '?',
            'location': 'Position non détectée',
            'rssi': 0,
            'confidence': 'Aucune'
        }

def locate_position(aps: List[Dict]) -> Dict:
    if not aps:
        return {
            'success': False,
            'error': 'No APs detected',
            'timestamp': datetime.now().isoformat()
        }
    
    rssi_result = simple_rssi_matching(aps)
    centroid_result = triangulate(aps)
    room_result = locate_room(aps)
    
    if rssi_result and rssi_result['confidence'] > 0.3:
        result = {
            'success': True,
            'lat': rssi_result['lat'],
            'lon': rssi_result['lon'],
            'room': rssi_result['room'],
            'floor': rssi_result['floor'],
            'location': rssi_result['location'],
            'method': 'RSSI Matching',
            'confidence': f"{rssi_result['confidence']*100:.0f}%",
            'matched_aps': rssi_result['matched_aps'],
            'details': [{'mac': ap['mac'], 'rssi': ap['rssi']} for ap in aps[:5]],
            'timestamp': datetime.now().isoformat()
        }
    elif centroid_result:
        result = {
            'success': True,
            'lat': centroid_result['lat'],
            'lon': centroid_result['lon'],
            'room': room_result['room'],
            'floor': room_result['floor'],
            'location': room_result['location'],
            'method': 'Triangulation',
            'confidence': f"{min(centroid_result['matched_aps'] / 3 * 100, 100):.0f}%",
            'matched_aps': centroid_result['matched_aps'],
            'details': centroid_result['details'],
            'timestamp': datetime.now().isoformat()
        }
    else:
        result = {
            'success': False,
            'error': 'Insufficient data',
            'timestamp': datetime.now().isoformat()
        }
    
    return result

def on_message(client, userdata, msg):
    global current_position
    try:
        payload = json.loads(msg.payload.decode())
        
        if 'uplink_message' not in payload:
            return
        
        b64_data = payload['uplink_message']['frm_payload']
        aps = decode_payload(b64_data)
        
        if not aps:
            return
        
        result = locate_position(aps)
        current_position = result
        
        if result['success']:
            print(f"📍 Position: {result['room']} (Étage {result['floor']}) - {result['method']} - {result['confidence']}")
        
        import asyncio
        for ws in websocket_connections[:]:
            try:
                asyncio.create_task(ws.send_json(result))
            except:
                websocket_connections.remove(ws)
        
    except Exception as e:
        print(f"✗ Error: {e}")

@app.on_event("startup")
async def startup():
    load_database()
    
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    
    client.username_pw_set("project1-sniffer@ttn", "NNSXS.URZ75UUXP7WCFQJ33P4XTXTL4D4YXK2D2A5P63A.AAKKN5KZOCIFHZ6KA654WBQXXYUOTKUONITP5DEJKMAP2EONXMRQ")
    client.on_message = on_message
    
    try:
        client.connect("eu1.cloud.thethings.network", 1883, 60)
        client.subscribe("v3/project1-sniffer@ttn/devices/esp32-lora-sniffer/up")
        client.loop_start()
        print("✓ Connected to TTN")
    except Exception as e:
        print(f"✗ MQTT Error: {e}")

@app.get("/")
async def root():
    return FileResponse('../frontend/index.html')

@app.get("/api/status")
async def status():
    return {
        "status": "running",
        "aps_loaded": len(ap_database),
        "fingerprints": len(fingerprint_data)
    }

@app.get("/api/position")
async def get_position():
    if current_position:
        return current_position
    return {"success": False, "error": "No position data yet"}

@app.get("/api/aps")
async def get_aps():
    return {
        "total": len(ap_database),
        "aps": [
            {
                "mac": mac,
                **data
            }
            for mac, data in ap_database.items()
        ]
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)