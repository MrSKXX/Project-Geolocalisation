from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import paho.mqtt.client as mqtt
import json
import base64
import pandas as pd
from typing import Dict, List
import math
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ap_database: Dict = {}
current_position = None
websocket_connections: List[WebSocket] = []

def load_database():
    global ap_database
    try:
        df = pd.read_csv('../data/ap_database.csv', sep=';')
        for _, row in df.iterrows():
            mac = row['MAC'].lower()
            ap_database[mac] = {
                'ssid': row['SSID'],
                'lat': float(row['CurrentLatitude']),
                'lon': float(row['CurrentLongitude']),
                'location': row['Location'],
                'floor': str(row['Floor']),
                'room': str(row['Room'])
            }
        print(f"✅ Loaded {len(ap_database)} APs")
    except Exception as e:
        print(f"❌ Error loading database: {e}")

def rssi_to_distance(rssi: int) -> float:
    rssi_at_1m = -40
    n = 2.7
    return math.pow(10, (rssi_at_1m - rssi) / (10 * n))

def decode_payload(b64_payload: str) -> List[Dict]:
    buf = base64.b64decode(b64_payload)
    
    if len(buf) != 21:
        return []
    
    aps = []
    for i in range(3):
        offset = i * 7
        mac_bytes = buf[offset:offset+6]
        rssi_byte = buf[offset+6]
        
        rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
        mac = ':'.join(f'{b:02x}' for b in mac_bytes)
        
        if mac != '00:00:00:00:00:00':
            aps.append({'mac': mac, 'rssi': rssi})
    
    return aps

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
            'confidence': f"{(matched / 3.0 * 100):.0f}%",
            'details': matched_details,
            'timestamp': datetime.now().isoformat()
        }
    else:
        return {'success': False, 'error': 'No matching APs found'}

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
            'confidence': confidence,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
    else:
        return {
            'room': 'Inconnue',
            'floor': '?',
            'location': 'Position non détectée',
            'rssi': 0,
            'confidence': 'Aucune',
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }

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
        
        position = triangulate(aps)
        room_info = locate_room(aps)
        
        result = {
            **position,
            'room_info': room_info
        }
        
        current_position = result
        print(f"📍 Position: {room_info['room']} (Étage {room_info['floor']})")
        
        for ws in websocket_connections:
            try:
                import asyncio
                asyncio.create_task(ws.send_json(result))
            except:
                websocket_connections.remove(ws)
        
    except Exception as e:
        print(f"❌ Error: {e}")

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
        print("✅ Connected to TTN")
    except Exception as e:
        print(f"❌ MQTT Error: {e}")

@app.get("/")
async def root():
    return FileResponse('../frontend/index.html')

@app.get("/api/status")
async def status():
    return {"status": "running", "aps_loaded": len(ap_database)}

@app.get("/api/position")
async def get_position():
    if current_position:
        return current_position
    return {"error": "No position data yet"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        websocket_connections.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)