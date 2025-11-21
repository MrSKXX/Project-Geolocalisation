"""
Système de Géolocalisation WiFi Sans GPS
Backend FastAPI - Polytech Sorbonne

Architecture:
    ESP32 → LoRaWAN → TTN → MQTT → Backend → WebSocket → Frontend
    
Fonctionnement:
    1. Charge les fingerprints WiFi depuis SQLite
    2. Reçoit les scans WiFi de l'ESP32 via MQTT/TTN
    3. Compare les RSSI avec la base de données (RSSI Matching)
    4. Calcule la position et envoie au frontend en temps réel
"""

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

# ============================================================================
# CONFIGURATION FASTAPI
# ============================================================================

app = FastAPI()

# Configuration CORS pour permettre les requêtes depuis le frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Accepte toutes les origines (à restreindre en production)
    allow_credentials=True,
    allow_methods=["*"],          # Accepte toutes les méthodes HTTP
    allow_headers=["*"],          # Accepte tous les headers
)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

DB_PATH = '../tools/geolocation.db'              # Chemin vers la base SQLite
ap_database: Dict = {}                           # Dictionnaire {mac: {room, lat, lon, ...}}
fingerprint_data = []                            # Liste de tous les échantillons collectés
current_position = None                          # Dernière position calculée
websocket_connections: List[WebSocket] = []      # Liste des connexions WebSocket actives

# ============================================================================
# CHARGEMENT DE LA BASE DE DONNÉES
# ============================================================================

def load_database():
    """
    Charge les fingerprints WiFi depuis SQLite et construit deux structures:
    
    1. fingerprint_data: Liste de TOUS les échantillons individuels
       Exemple: [{room: '201', mac: 'aa:bb:cc', rssi: -65, lat: 48.84, lon: 2.35}, ...]
       
    2. ap_database: Dictionnaire des APs uniques avec position moyenne par salle
       Exemple: {'aa:bb:cc': {room: '201', lat: 48.84, lon: 2.35}}
       
    Processus:
        - Lit tous les fingerprints de la table SQLite
        - Agrège par salle pour calculer les positions moyennes
        - Crée un dictionnaire rapide pour la recherche d'APs
    """
    global ap_database, fingerprint_data
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Étape 1: Charger TOUS les fingerprints (échantillons individuels)
        c.execute("SELECT * FROM fingerprints")
        for row in c.fetchall():
            fingerprint_data.append({
                'room': row[1],           # Numéro de salle (ex: '201')
                'floor': row[2],          # Étage (ex: '2')
                'location': row[3],       # Description (ex: 'Salle 201')
                'lat': row[4],           # Latitude GPS
                'lon': row[5],           # Longitude GPS
                'mac': row[6].lower(),   # Adresse MAC en minuscules pour uniformité
                'ssid': row[7],          # Nom du réseau WiFi
                'rssi': row[8],          # Force du signal (-100 à 0 dBm)
                'timestamp': row[9]      # Date/heure de collecte
            })
        
        # Étape 2: Agréger les données par salle pour calculer les moyennes
        # Structure: {'201_2': {macs: set(), lat: [48.84, 48.84, ...], lon: [...], ...}}
        rooms_data = defaultdict(lambda: {
            'macs': set(),           # Ensemble des MACs détectés dans cette salle
            'lat': [],              # Liste de toutes les latitudes enregistrées
            'lon': [],              # Liste de toutes les longitudes enregistrées
            'location': '',         # Description de la salle
            'floor': ''            # Étage
        })
        
        # Parcourir tous les fingerprints et grouper par salle
        for fp in fingerprint_data:
            key = f"{fp['room']}_{fp['floor']}"   # Clé unique: "201_2"
            rooms_data[key]['macs'].add(fp['mac'])
            rooms_data[key]['lat'].append(fp['lat'])
            rooms_data[key]['lon'].append(fp['lon'])
            rooms_data[key]['location'] = fp['location']
            rooms_data[key]['floor'] = fp['floor']
        
        # Étape 3: Créer ap_database avec positions moyennes
        for room_key, data in rooms_data.items():
            room, floor = room_key.split('_')
            
            # Calculer la position moyenne de la salle
            avg_lat = sum(data['lat']) / len(data['lat'])
            avg_lon = sum(data['lon']) / len(data['lon'])
            
            # Pour chaque MAC détecté dans cette salle, l'ajouter au dictionnaire
            for mac in data['macs']:
                if mac not in ap_database:  # Éviter d'écraser si MAC existe déjà
                    ap_database[mac] = {
                        'ssid': 'Unknown',          # SSID non stocké dans les fingerprints
                        'lat': avg_lat,            # Position moyenne de la salle
                        'lon': avg_lon,
                        'location': data['location'],
                        'floor': floor,
                        'room': room
                    }
        
        conn.close()
        print(f"✓ Loaded {len(ap_database)} unique APs from {len(fingerprint_data)} fingerprints")
        
    except Exception as e:
        print(f"✗ Error loading database: {e}")

# ============================================================================
# FONCTIONS DE CALCUL DE DISTANCE
# ============================================================================

def rssi_to_distance(rssi: int) -> float:
    """
    Convertit un RSSI (force du signal) en distance estimée.
    
    Formule du modèle de propagation logarithmique:
        distance = 10^((RSSI_ref - RSSI) / (10 * n))
    
    Paramètres:
        - rssi_at_1m = -40 dBm : RSSI de référence à 1 mètre
        - n = 2.7 : Exposant de perte de propagation (2.0 = espace libre, 3.0+ = bâtiment)
        
    Exemple:
        RSSI = -65 dBm → distance ≈ 10 mètres
        RSSI = -50 dBm → distance ≈ 3 mètres
    
    Note: Cette estimation est très approximative car le RSSI varie selon:
        - Les murs et obstacles
        - Les interférences WiFi
        - L'orientation de l'antenne
    """
    rssi_at_1m = -40   # RSSI de référence à 1 mètre
    n = 2.7           # Exposant de perte (environnement intérieur)
    return math.pow(10, (rssi_at_1m - rssi) / (10 * n))

# ============================================================================
# DÉCODAGE DU PAYLOAD LORAWAN
# ============================================================================

def decode_payload(b64_payload: str) -> List[Dict]:
    """
    Décode le payload Base64 reçu de TTN en liste d'Access Points.
    
    Format du payload binaire:
        Chaque AP = 7 octets :
        - 6 octets : Adresse MAC (ex: 1E:92:9B:E8:5C:D9)
        - 1 octet  : RSSI signé (-128 à 127 dBm)
        
    Exemple de payload (3 APs = 21 octets):
        [1E 92 9B E8 5C D9 BF] [76 A0 74 60 69 BD BA] [86 39 8E 64 5A 8E B5]
         └─────── MAC ──────┘ │  └─────── MAC ──────┘ │  └─────── MAC ──────┘ │
                            RSSI                    RSSI                    RSSI
    
    Processus:
        1. Décode Base64 → bytes
        2. Vérifie que la longueur est multiple de 7
        3. Extrait chaque bloc de 7 octets
        4. Convertit MAC en format hexadécimal
        5. Convertit RSSI en valeur signée
    
    Retourne:
        Liste de dictionnaires: [{'mac': '1e:92:9b:e8:5c:d9', 'rssi': -65}, ...]
    """
    try:
        # Décoder Base64 en bytes
        buf = base64.b64decode(b64_payload)
        
        # Vérifier que la longueur est valide (multiple de 7)
        if len(buf) % 7 != 0:
            return []
        
        num_aps = len(buf) // 7   # Nombre d'APs dans le payload
        aps = []
        
        # Extraire chaque AP (bloc de 7 octets)
        for i in range(num_aps):
            offset = i * 7
            
            # Extraire les 6 octets de la MAC
            mac_bytes = buf[offset:offset+6]
            
            # Extraire l'octet RSSI (7ème octet)
            rssi_byte = buf[offset+6]
            
            # Convertir RSSI en valeur signée (0-127 positif, 128-255 négatif)
            rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
            
            # Formater MAC en hexadécimal avec séparateurs ':'
            mac = ':'.join(f'{b:02x}' for b in mac_bytes)
            
            # Ignorer les MACs vides (padding)
            if mac != '00:00:00:00:00:00':
                aps.append({'mac': mac, 'rssi': rssi})
        
        return aps
    
    except Exception as e:
        print(f"✗ Decode error: {e}")
        return []

# ============================================================================
# ALGORITHME DE LOCALISATION : RSSI MATCHING
# ============================================================================

def simple_rssi_matching(aps: List[Dict]) -> Dict:
    """
    Algorithme principal de localisation par comparaison de RSSI.
    
    PRINCIPE:
        Pour chaque salle dans la base de données, calcule un score de similarité
        en comparant les RSSI détectés maintenant avec les RSSI moyens enregistrés.
        La salle avec le meilleur score est retournée.
    
    ÉTAPES:
        1. Préparer les données de référence (moyennes RSSI par salle)
        2. Pour chaque salle, calculer un score de similarité
        3. Retourner la salle avec le meilleur score
    
    CALCUL DU SCORE:
        Pour chaque MAC détecté:
            diff = |RSSI_détecté - RSSI_moyen_en_base|
            score = 100 - diff
        
        Score final = moyenne des scores de tous les MACs matchés
        
    EXEMPLE CONCRET:
        Tu es en salle 201, ESP32 détecte:
            aa:bb:cc → -65 dBm
            dd:ee:ff → -70 dBm
        
        En base pour salle 201 (moyenne de 30 échantillons):
            aa:bb:cc → -63 dBm moyen
            dd:ee:ff → -68 dBm moyen
        
        Calcul:
            diff1 = |-65 - (-63)| = 2  →  score1 = 100 - 2 = 98
            diff2 = |-70 - (-68)| = 2  →  score2 = 100 - 2 = 98
            score_moyen = (98 + 98) / 2 = 98
            confidence = 98%
        
        En base pour salle 203:
            aa:bb:cc → -72 dBm moyen
            dd:ee:ff → non détecté
        
        Calcul:
            diff1 = |-65 - (-72)| = 7  →  score1 = 100 - 7 = 93
            score_moyen = 93 / 1 = 93
            confidence = 93%
        
        Résultat: Salle 201 gagne (98% > 93%) ✓
    """
    best_match = None                    # Meilleure salle trouvée
    best_score = -float('inf')           # Meilleur score (initialisé à -infini)
    
    # Étape 1: Préparer les données de référence par salle
    # Structure: {'201_2': {rssi_by_mac: {'aa:bb:cc': [-65, -63, -67, ...]}, lat: [...], ...}}
    rooms_data = defaultdict(lambda: {
        'rssi_by_mac': defaultdict(list),  # Dictionnaire de listes de RSSI par MAC
        'lat': [],                         # Liste des latitudes
        'lon': [],                         # Liste des longitudes
        'location': '',                    # Description
        'floor': ''                       # Étage
    })
    
    # Remplir rooms_data avec tous les fingerprints collectés
    for fp in fingerprint_data:
        key = f"{fp['room']}_{fp['floor']}"
        rooms_data[key]['rssi_by_mac'][fp['mac']].append(fp['rssi'])
        rooms_data[key]['lat'].append(fp['lat'])
        rooms_data[key]['lon'].append(fp['lon'])
        rooms_data[key]['location'] = fp['location']
        rooms_data[key]['floor'] = fp['floor']
    
    # Créer un dictionnaire des MACs détectés maintenant
    # Exemple: {'aa:bb:cc': -65, 'dd:ee:ff': -70}
    detected_macs = {ap['mac'].lower(): ap['rssi'] for ap in aps}
    
    # Étape 2: Pour chaque salle, calculer un score de similarité
    for room_key, room_data in rooms_data.items():
        score = 0           # Score total pour cette salle
        matches = 0         # Nombre de MACs matchés
        
        # Comparer chaque MAC connu de cette salle avec les MACs détectés
        for mac, rssi_list in room_data['rssi_by_mac'].items():
            if mac in detected_macs:
                # Calculer le RSSI moyen enregistré pour ce MAC dans cette salle
                avg_rssi = sum(rssi_list) / len(rssi_list)
                
                # Calculer la différence absolue
                diff = abs(detected_macs[mac] - avg_rssi)
                
                # Convertir en score (plus diff est petit, plus score est haut)
                score += (100 - diff)
                matches += 1
        
        # Si au moins un MAC a matché, calculer le score moyen
        if matches > 0:
            score = score / matches              # Score moyen
            
            # Si c'est le meilleur score jusqu'à présent
            if score > best_score:
                best_score = score
                room, floor = room_key.split('_')
                
                best_match = {
                    'room': room,
                    'floor': floor,
                    'lat': sum(room_data['lat']) / len(room_data['lat']),    # Position moyenne
                    'lon': sum(room_data['lon']) / len(room_data['lon']),
                    'location': room_data['location'],
                    'confidence': min(score / 100, 1.0),                      # Confiance (0-1)
                    'matched_aps': matches                                    # Nombre d'APs matchés
                }
    
    return best_match

# ============================================================================
# ALGORITHME DE LOCALISATION : TRIANGULATION (FALLBACK)
# ============================================================================

def triangulate(aps: List[Dict]) -> Dict:
    """
    Méthode de triangulation par centroïde pondéré (utilisée en fallback).
    
    PRINCIPE:
        Calculer la position comme moyenne pondérée des positions des APs connus,
        où le poids dépend de la distance estimée (RSSI → distance).
    
    FORMULE:
        lat = Σ(lat_i × poids_i) / Σ(poids_i)
        lon = Σ(lon_i × poids_i) / Σ(poids_i)
        
        où poids = 2 / distance^0.65
    
    UTILISATION:
        Cette méthode est utilisée quand RSSI Matching a une confiance < 30%
        (ex: si peu d'APs sont dans la base de données)
    """
    numerateur_x = 0        # Somme pondérée des latitudes
    numerateur_y = 0        # Somme pondérée des longitudes
    denominateur = 0        # Somme des poids
    matched = 0             # Nombre d'APs matchés
    matched_details = []    # Détails des APs pour le frontend
    
    # Pour chaque AP détecté
    for ap in aps:
        mac_lower = ap['mac'].lower()
        known = ap_database.get(mac_lower)
        
        if known:
            # Estimer la distance depuis le RSSI
            distance = rssi_to_distance(ap['rssi'])
            
            # Calculer le poids (inversement proportionnel à la distance)
            # Plus on est proche, plus le poids est élevé
            weight = 2 / math.pow(distance, 0.65)
            
            # Accumuler pour le calcul du centroïde pondéré
            numerateur_x += known['lat'] * weight
            numerateur_y += known['lon'] * weight
            denominateur += weight
            matched += 1
            
            # Enregistrer les détails pour le frontend
            matched_details.append({
                'mac': ap['mac'],
                'ssid': known['ssid'],
                'rssi': ap['rssi'],
                'distance': f"{distance:.2f}m"
            })
    
    # Si au moins un AP a été trouvé, calculer la position
    if denominateur > 0:
        return {
            'success': True,
            'lat': numerateur_x / denominateur,      # Position moyenne pondérée
            'lon': numerateur_y / denominateur,
            'matched_aps': matched,
            'details': matched_details
        }
    else:
        return None

# ============================================================================
# DÉTECTION DE SALLE PAR AP LE PLUS FORT (FALLBACK)
# ============================================================================

def locate_room(aps: List[Dict]) -> Dict:
    """
    Méthode simple: retourne la salle de l'AP avec le signal le plus fort.
    
    PRINCIPE:
        L'AP avec le RSSI le plus élevé (le moins négatif) est probablement
        le plus proche, donc on utilise sa salle comme estimation.
    
    UTILISATION:
        Utilisée en combinaison avec triangulate() quand RSSI Matching échoue.
    """
    best_match = None
    best_rssi = -100        # Initialiser au pire RSSI possible
    
    # Trouver l'AP avec le signal le plus fort
    for ap in aps:
        mac_lower = ap['mac'].lower()
        known = ap_database.get(mac_lower)
        
        if known and ap['rssi'] > best_rssi:
            best_rssi = ap['rssi']
            best_match = known
    
    if best_match:
        # Déterminer la confiance selon la force du signal
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

# ============================================================================
# FONCTION PRINCIPALE DE LOCALISATION
# ============================================================================

def locate_position(aps: List[Dict]) -> Dict:
    """
    Fonction principale qui orchestre les différentes méthodes de localisation.
    
    STRATÉGIE DE LOCALISATION (par ordre de priorité):
        1. RSSI Matching (si confiance > 30%)
           → Méthode la plus précise quand la base est bien calibrée
           
        2. Triangulation + locate_room (fallback)
           → Utilisé quand peu d'APs sont en base
           
        3. Erreur (aucun AP détecté)
    
    Paramètres:
        aps: Liste des APs détectés par l'ESP32
             Format: [{'mac': 'aa:bb:cc', 'rssi': -65}, ...]
    
    Retourne:
        Dictionnaire avec la position calculée:
        {
            'success': True,
            'lat': 48.845129,
            'lon': 2.356774,
            'room': '201',
            'floor': '2',
            'location': 'Salle 201',
            'method': 'RSSI Matching',
            'confidence': '97%',
            'matched_aps': 3,
            'details': [{'mac': '...', 'rssi': -65}, ...],
            'timestamp': '2025-01-20T17:30:00'
        }
    """
    # Vérifier qu'il y a des APs détectés
    if not aps:
        return {
            'success': False,
            'error': 'No APs detected',
            'timestamp': datetime.now().isoformat()
        }
    
    # Essayer les 3 méthodes de localisation
    rssi_result = simple_rssi_matching(aps)      # Méthode 1: RSSI Matching
    centroid_result = triangulate(aps)           # Méthode 2: Triangulation
    room_result = locate_room(aps)               # Méthode 3: AP le plus fort
    
    # STRATÉGIE 1: Utiliser RSSI Matching si confiance suffisante (> 30%)
    if rssi_result and rssi_result['confidence'] > 0.3:
        result = {
            'success': True,
            'lat': rssi_result['lat'],
            'lon': rssi_result['lon'],
            'room': rssi_result['room'],
            'floor': rssi_result['floor'],
            'location': rssi_result['location'],
            'method': 'RSSI Matching',                                      # Méthode utilisée
            'confidence': f"{rssi_result['confidence']*100:.0f}%",          # Confiance en %
            'matched_aps': rssi_result['matched_aps'],                      # Nombre d'APs matchés
            'details': [{'mac': ap['mac'], 'rssi': ap['rssi']} for ap in aps[:5]],  # Top 5 APs
            'timestamp': datetime.now().isoformat()
        }
    
    # STRATÉGIE 2: Utiliser Triangulation + locate_room si RSSI Matching échoue
    elif centroid_result:
        result = {
            'success': True,
            'lat': centroid_result['lat'],
            'lon': centroid_result['lon'],
            'room': room_result['room'],                                    # Salle de l'AP le plus fort
            'floor': room_result['floor'],
            'location': room_result['location'],
            'method': 'Triangulation',                                      # Méthode fallback
            'confidence': f"{min(centroid_result['matched_aps'] / 3 * 100, 100):.0f}%",
            'matched_aps': centroid_result['matched_aps'],
            'details': centroid_result['details'],
            'timestamp': datetime.now().isoformat()
        }
    
    # STRATÉGIE 3: Échec complet (aucun AP connu)
    else:
        result = {
            'success': False,
            'error': 'Insufficient data',
            'timestamp': datetime.now().isoformat()
        }
    
    return result

# ============================================================================
# RÉCEPTION DES MESSAGES MQTT (TTN)
# ============================================================================

def on_message(client, userdata, msg):
    """
    Callback appelé quand un message MQTT est reçu de The Things Network.
    
    FLOW:
        1. TTN reçoit le paquet LoRa de l'ESP32
        2. TTN publie sur MQTT: v3/.../devices/.../up
        3. Cette fonction est déclenchée
        4. Décode le payload → liste d'APs
        5. Calcule la position
        6. Envoie au frontend via WebSocket
    
    Format du message TTN:
        {
            "uplink_message": {
                "frm_payload": "HpKb6FzZ5...",  ← Payload Base64
                "rx_metadata": [...],
                "settings": {...}
            }
        }
    """
    global current_position
    
    try:
        # Décoder le JSON MQTT
        payload = json.loads(msg.payload.decode())
        
        # Vérifier que c'est un uplink (message montant)
        if 'uplink_message' not in payload:
            return
        
        # Extraire le payload Base64
        b64_data = payload['uplink_message']['frm_payload']
        
        # Décoder le payload → liste d'APs
        aps = decode_payload(b64_data)
        
        if not aps:
            return
        
        # Calculer la position avec les APs détectés
        result = locate_position(aps)
        current_position = result
        
        # Afficher dans le terminal du backend
        if result['success']:
            print(f"📍 Position: {result['room']} (Étage {result['floor']}) - {result['method']} - {result['confidence']}")
        
        # Envoyer au frontend via WebSocket (temps réel)
        import asyncio
        for ws in websocket_connections[:]:
            try:
                asyncio.create_task(ws.send_json(result))
            except:
                websocket_connections.remove(ws)
        
    except Exception as e:
        print(f"✗ Error: {e}")

# ============================================================================
# DÉMARRAGE DU SERVEUR
# ============================================================================

@app.on_event("startup")
async def startup():
    """
    Fonction appelée au démarrage du serveur FastAPI.
    
    ACTIONS:
        1. Charger la base de données SQLite
        2. Se connecter à TTN via MQTT
        3. S'abonner au topic des messages ESP32
    """
    # Charger les fingerprints depuis SQLite
    load_database()
    
    # Créer le client MQTT
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    
    # Configurer les identifiants TTN
    client.username_pw_set(
        "project1-sniffer@ttn",
        "NNSXS.URZ75UUXP7WCFQJ33P4XTXTL4D4YXK2D2A5P63A.AAKKN5KZOCIFHZ6KA654WBQXXYUOTKUONITP5DEJKMAP2EONXMRQ"
    )
    
    # Définir le callback pour les messages reçus
    client.on_message = on_message
    
    try:
        # Se connecter au broker MQTT de TTN
        client.connect("eu1.cloud.thethings.network", 1883, 60)
        
        # S'abonner au topic des uplinks de l'ESP32
        # Format: v3/{application_id}/devices/{device_id}/up
        client.subscribe("v3/project1-sniffer@ttn/devices/esp32-lora-sniffer/up")
        
        # Démarrer la boucle MQTT en arrière-plan
        client.loop_start()
        
        print("✓ Connected to TTN")
    except Exception as e:
        print(f"✗ MQTT Error: {e}")

# ============================================================================
# ROUTES HTTP (API REST)
# ============================================================================

@app.get("/")
async def root():
    """Sert le fichier HTML du frontend"""
    return FileResponse('../frontend/index.html')

@app.get("/api/status")
async def status():
    """
    Retourne le statut du système.
    
    Utilisé par le frontend pour afficher le nombre d'APs chargés.
    """
    return {
        "status": "running",
        "aps_loaded": len(ap_database),          # Nombre d'APs uniques en base
        "fingerprints": len(fingerprint_data)    # Nombre total d'échantillons
    }

@app.get("/api/position")
async def get_position():
    """
    Retourne la dernière position calculée.
    
    Utilisé par le frontend au chargement de la page pour afficher
    la dernière position connue (avant de recevoir les updates WebSocket).
    """
    if current_position:
        return current_position
    return {"success": False, "error": "No position data yet"}

@app.get("/api/aps")
async def get_aps():
    """
    Retourne la liste de tous les APs connus.
    
    Utilisé par le frontend pour afficher les points bleus sur la carte
    (position des APs connus).
    """
    return {
        "total": len(ap_database),
        "aps": [
            {
                "mac": mac,
                **data            # Unpacking: inclut ssid, lat, lon, room, floor
            }
            for mac, data in ap_database.items()
        ]
    }

# ============================================================================
# WEBSOCKET (COMMUNICATION TEMPS RÉEL)
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Endpoint WebSocket pour la communication temps réel avec le frontend.
    
    FONCTIONNEMENT:
        1. Le frontend se connecte à ws://localhost:8000/ws
        2. La connexion est ajoutée à websocket_connections
        3. Quand une nouvelle position est calculée (on_message),
           elle est envoyée à toutes les connexions actives
        4. Le frontend met à jour la carte et le dashboard instantanément
    
    AVANTAGES vs HTTP polling:
        - Latence minimale (~10ms vs 1000ms)
        - Moins de charge serveur (1 connexion vs 1 requête/seconde)
        - Bidirectionnel (si besoin de commandes futures)
    """
    # Accepter la connexion WebSocket
    await websocket.accept()
    
    # Ajouter à la liste des connexions actives
    websocket_connections.append(websocket)
    
    try:
        # Boucle infinie pour garder la connexion ouverte
        while True:
            await websocket.receive_text()   # Attendre des messages (non utilisé actuellement)
    except:
        # En cas de déconnexion, retirer de la liste
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)

# ============================================================================
# POINT D'ENTRÉE
# ============================================================================

if __name__ == "__main__":
    """
    Lance le serveur Uvicorn (serveur ASGI pour FastAPI).
    
    Commande équivalente:
        uvicorn main:app --host 0.0.0.0 --port 8000
    
    Le serveur écoute sur:
        - http://0.0.0.0:8000 (API REST)
        - ws://0.0.0.0:8000/ws (WebSocket)
    """
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)