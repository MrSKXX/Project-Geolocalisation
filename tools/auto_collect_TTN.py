"""
Script de Collecte Automatique de Fingerprints WiFi
Polytech Sorbonne - Géolocalisation sans GPS

OBJECTIF:
    Collecter des échantillons WiFi (RSSI) pour chaque salle du bâtiment
    afin de créer une base de données de référence pour la localisation.

PRINCIPE:
    1. Se connecte à The Things Network via MQTT
    2. Reçoit les scans WiFi de l'ESP32 toutes les 60 secondes
    3. Enregistre chaque scan dans SQLite avec la position GPS de la salle
    4. Répète pour plusieurs salles et positions

UTILISATION:
    python3 auto_collect_TTN.py
    
    Puis pour chaque position:
        - Entrer le numéro de salle
        - Entrer l'étage
        - Entrer les coordonnées GPS (Google Maps)
        - Attendre 5 échantillons (~5 minutes)
        - Passer à la position suivante ou quitter

RÉSULTAT:
    Base SQLite avec structure:
    | room | floor | lat | lon | mac | rssi | timestamp |
    |------|-------|-----|-----|-----|------|-----------|
    | 201  | 2     |48.84|2.35 | aa..| -65  | 2025-...  |
    
    Cette base sera utilisée par le backend pour comparer les RSSI
    détectés en temps réel et déterminer la position.
"""

import paho.mqtt.client as mqtt
import json
import base64
import sqlite3
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

DB_PATH = 'geolocation.db'              # Chemin de la base de données SQLite

# Variables globales pour la position actuelle de collecte
current_room = None                      # Numéro de salle (ex: '201')
current_floor = None                     # Étage (ex: '2')
current_location = None                  # Description (ex: 'Salle 201')
current_lat = None                       # Latitude GPS
current_lon = None                       # Longitude GPS

sample_count = 0                         # Compteur d'échantillons collectés
target_samples = 5                       # Nombre d'échantillons à collecter par position

# ============================================================================
# INITIALISATION DE LA BASE DE DONNÉES
# ============================================================================

def init_db():
    """
    Crée la table SQLite pour stocker les fingerprints WiFi.
    
    STRUCTURE DE LA TABLE:
        - id: Identifiant unique auto-incrémenté
        - room: Numéro de salle (ex: '201', '203')
        - floor: Étage (ex: '2', '3')
        - location: Description textuelle (ex: 'Salle 201')
        - lat: Latitude GPS (ex: 48.845129)
        - lon: Longitude GPS (ex: 2.356774)
        - mac: Adresse MAC de l'Access Point (ex: '1e:92:9b:e8:5c:d9')
        - ssid: Nom du réseau WiFi (stocké comme 'Unknown' car non récupéré)
        - rssi: Force du signal en dBm (ex: -65)
        - timestamp: Date/heure de collecte (format ISO: '2025-01-20T17:30:00')
    
    UTILISATION DES DONNÉES:
        Chaque ligne = 1 détection d'AP lors d'un scan
        Si 3 APs détectés → 3 lignes insérées
        Si 5 scans × 3 APs → 15 lignes pour cette position
        
    EXEMPLE DE DONNÉES:
        room='201', mac='1e:92:9b:e8:5c:d9', rssi=-65, timestamp='2025-01-20 17:10:00'
        room='201', mac='76:a0:74:60:bb:9d', rssi=-70, timestamp='2025-01-20 17:10:00'
        room='201', mac='1e:92:9b:e8:5c:d9', rssi=-63, timestamp='2025-01-20 17:11:00'  ← Nouveau scan
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Créer la table si elle n'existe pas déjà
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

# ============================================================================
# DÉCODAGE DU PAYLOAD LORAWAN
# ============================================================================

def decode_payload(b64_payload):
    """
    Décode le payload Base64 reçu de TTN en liste d'Access Points.
    
    FORMAT DU PAYLOAD BINAIRE:
        L'ESP32 encode chaque AP sur 7 octets:
        - Octets 0-5 : Adresse MAC (6 octets)
        - Octet 6    : RSSI signé (1 octet, valeur -128 à 127)
        
    EXEMPLE DE PAYLOAD (3 APs = 21 octets):
        Hex: 1E929BE85CD9BF76A0746069BDBA8639...
        
        Décomposition:
        [1E 92 9B E8 5C D9] [BF] │ [76 A0 74 60 69 BD] [BA] │ ...
         └─────── MAC ──────┘  │   └─────── MAC ──────┘  │
                            RSSI=-65                   RSSI=-70
    
    PROCESSUS:
        1. Décoder Base64 → bytes bruts
        2. Diviser par blocs de 7 octets
        3. Pour chaque bloc:
           - Extraire 6 octets pour la MAC
           - Extraire 1 octet pour le RSSI
           - Convertir RSSI en valeur signée (gérer les nombres négatifs)
           - Formater MAC avec séparateurs ':'
        4. Ignorer les MACs nulles (padding)
    
    Paramètres:
        b64_payload (str): Payload Base64 depuis TTN
        
    Retourne:
        list: Liste de dictionnaires [{'mac': '1e:92:9b:e8:5c:d9', 'rssi': -65}, ...]
    """
    # Étape 1: Décoder Base64 en bytes
    buf = base64.b64decode(b64_payload)
    
    # Étape 2: Calculer le nombre d'APs (chaque AP = 7 octets)
    num_aps = len(buf) // 7
    aps = []
    
    # Étape 3: Extraire chaque AP
    for i in range(num_aps):
        offset = i * 7                    # Position de départ dans le buffer
        
        # Extraire les 6 octets de la MAC
        mac_bytes = buf[offset:offset+6]
        
        # Extraire l'octet RSSI (7ème octet du bloc)
        rssi_byte = buf[offset+6]
        
        # Convertir en RSSI signé:
        # - Si < 128 : valeur positive (rare, signal très fort)
        # - Si >= 128 : soustraire 256 pour obtenir la valeur négative
        # Exemple: 191 (0xBF) → 191 - 256 = -65 dBm
        rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
        
        # Formater MAC en hexadécimal avec séparateurs ':'
        # Exemple: [0x1E, 0x92, 0x9B, ...] → '1e:92:9b:...'
        mac = ':'.join(f'{b:02x}' for b in mac_bytes)
        
        # Ignorer les MACs nulles (utilisées comme padding par l'ESP32)
        if mac != '00:00:00:00:00:00':
            aps.append({'mac': mac, 'rssi': rssi})
    
    return aps

# ============================================================================
# SAUVEGARDE DES FINGERPRINTS
# ============================================================================

def save_fingerprints(aps):
    """
    Enregistre les APs détectés dans la base SQLite avec la position actuelle.
    
    FONCTIONNEMENT:
        Pour chaque AP détecté:
            - Insère une nouvelle ligne dans la table fingerprints
            - Associe l'AP à la position actuelle (room, floor, lat, lon)
            - Enregistre le RSSI mesuré
            - Ajoute un timestamp
    
    EXEMPLE:
        Position actuelle: Salle 201, Étage 2, GPS=(48.845129, 2.356774)
        APs reçus: [
            {'mac': '1e:92:9b:e8:5c:d9', 'rssi': -65},
            {'mac': '76:a0:74:60:bb:9d', 'rssi': -70}
        ]
        
        → 2 lignes insérées:
        | room | floor | lat      | lon     | mac              | rssi | timestamp        |
        |------|-------|----------|---------|------------------|------|------------------|
        | 201  | 2     | 48.84513 | 2.35677 | 1e:92:9b:e8:5c:d9| -65  | 2025-01-20 17:10 |
        | 201  | 2     | 48.84513 | 2.35677 | 76:a0:74:60:bb:9d| -70  | 2025-01-20 17:10 |
    
    PROGRESSION:
        - Incrémente le compteur d'échantillons
        - Affiche la progression (ex: "Échantillon 3/5")
        - Notifie quand target_samples est atteint
    
    Paramètres:
        aps (list): Liste d'APs détectés [{'mac': '...', 'rssi': -65}, ...]
    """
    global sample_count
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Horodatage au format ISO (ex: '2025-01-20T17:30:00')
    timestamp = datetime.now().isoformat()
    
    # Insérer chaque AP dans la base
    for ap in aps:
        c.execute('''INSERT INTO fingerprints 
                     (room, floor, location, lat, lon, mac, ssid, rssi, timestamp)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (current_room,           # Ex: '201'
                   current_floor,          # Ex: '2'
                   current_location,       # Ex: 'Salle 201'
                   current_lat,           # Ex: 48.845129
                   current_lon,           # Ex: 2.356774
                   ap['mac'],             # Ex: '1e:92:9b:e8:5c:d9'
                   'Unknown',             # SSID non récupéré (pas envoyé par ESP32)
                   ap['rssi'],            # Ex: -65
                   timestamp))            # Ex: '2025-01-20T17:30:00'
    
    conn.commit()
    conn.close()
    
    # Mettre à jour le compteur et afficher la progression
    sample_count += 1
    print(f"✓ Échantillon {sample_count}/{target_samples} enregistré ({len(aps)} APs)")
    
    # Notifier quand l'objectif est atteint
    if sample_count >= target_samples:
        print(f"\n🎉 {target_samples} échantillons collectés pour salle {current_room} !")
        print("Tapez 'next' pour changer de position ou 'quit' pour arrêter\n")

# ============================================================================
# CALLBACKS MQTT
# ============================================================================

def on_message(client, userdata, msg):
    """
    Callback appelé quand un message MQTT est reçu de TTN.
    
    DÉCLENCHEMENT:
        - L'ESP32 envoie un scan WiFi via LoRaWAN
        - TTN reçoit le paquet et le publie sur MQTT
        - Ce callback est immédiatement déclenché
    
    FLOW:
        1. Vérifie qu'une position est configurée (current_room != None)
        2. Parse le JSON MQTT
        3. Extrait le payload Base64
        4. Décode en liste d'APs
        5. Sauvegarde dans SQLite
    
    FORMAT DU MESSAGE TTN:
        {
            "uplink_message": {
                "frm_payload": "HpKb6FzZ5...",        ← Payload Base64
                "f_port": 1,
                "f_cnt": 42,
                "rx_metadata": [...],
                "settings": {...}
            },
            "received_at": "2025-01-20T17:30:00.123Z"
        }
    
    SÉCURITÉ:
        - Ignore les messages sans 'uplink_message' (downlinks, events)
        - Gère les erreurs de parsing JSON
        - Vérifie que des APs ont été décodés avant sauvegarde
    """
    # Ignorer si aucune position n'est configurée
    if current_room is None:
        return
    
    try:
        # Parser le JSON MQTT
        payload = json.loads(msg.payload.decode())
        
        # Vérifier que c'est un uplink (message montant de l'ESP32)
        if 'uplink_message' not in payload:
            return
        
        # Extraire le payload Base64
        b64_data = payload['uplink_message']['frm_payload']
        
        # Décoder en liste d'APs
        aps = decode_payload(b64_data)
        
        # Sauvegarder si des APs ont été détectés
        if aps:
            save_fingerprints(aps)
        
    except Exception as e:
        print(f"✗ Erreur: {e}")

def on_connect(client, userdata, flags, rc):
    """
    Callback appelé quand la connexion MQTT est établie.
    
    CODES DE RETOUR (rc):
        0 : Connexion réussie
        1 : Version de protocole incorrecte
        2 : Identifiant client rejeté
        3 : Serveur indisponible
        4 : Nom d'utilisateur/mot de passe incorrect
        5 : Non autorisé
    
    ACTION:
        Si rc == 0, s'abonner au topic des uplinks de l'ESP32
    """
    if rc == 0:
        print("✓ Connecté à TTN\n")
        
        # S'abonner au topic MQTT des uplinks
        # Format: v3/{application_id}/devices/{device_id}/up
        client.subscribe("v3/project1-sniffer@ttn/devices/esp32-lora-sniffer/up")
    else:
        print(f"✗ Connexion échouée: {rc}")

# ============================================================================
# CONFIGURATION DE LA POSITION
# ============================================================================

def set_location():
    """
    Demande à l'utilisateur les informations de la position actuelle.
    
    INFORMATIONS COLLECTÉES:
        1. Numéro de salle (ex: '201', '203')
        2. Étage (ex: '2', '3')
        3. Latitude GPS (ex: 48.845129)
        4. Longitude GPS (ex: 2.356774)
    
    OBTENIR LES COORDONNÉES GPS:
        Méthode 1 (Google Maps Desktop):
            1. Ouvrir https://www.google.com/maps
            2. Clic droit sur la position exacte dans la salle
            3. Cliquer sur les coordonnées affichées
            4. Format: 48.845129, 2.356774
            5. Copier/coller dans le terminal
        
        Méthode 2 (Google Maps Mobile):
            1. Appui long sur la position
            2. Coordonnées affichées en haut
            3. Copier et envoyer sur ordinateur
    
    IMPORTANCE DES COORDONNÉES PRÉCISES:
        - Utilisées pour calculer la position moyenne de chaque salle
        - Affichées sur la carte dans le frontend
        - Utilisées par l'algorithme de triangulation
        
        Si imprécises → marqueur mal placé sur la carte
        Si identiques pour toutes positions → perte de précision spatiale
    
    VALIDATION:
        - Vérifie que lat/lon sont des nombres valides
        - Retourne False si invalide (arrêt de la collecte)
    
    Retourne:
        bool: True si configuration réussie, False sinon
    """
    global current_room, current_floor, current_location, current_lat, current_lon, sample_count
    
    print("\n" + "="*60)
    print("NOUVELLE POSITION - SALLE 203")
    print("="*60)
    
    # Demander le numéro de salle
    current_room = input("Salle (ex: 203): ").strip()
    
    # Demander l'étage
    current_floor = input("Étage (ex: 2): ").strip()
    
    # Générer automatiquement la description
    current_location = f"Salle {current_room}"
    
    # Instructions pour obtenir les coordonnées GPS
    print("\n📍 Ouvre Google Maps et trouve la position exacte")
    print("   Clic droit sur la carte → Copie les coordonnées\n")
    
    # Demander latitude et longitude
    lat_str = input("Latitude: ").strip()
    lon_str = input("Longitude: ").strip()
    
    # Valider les coordonnées
    try:
        current_lat = float(lat_str)
        current_lon = float(lon_str)
        print(f"✓ Coordonnées validées: {current_lat}, {current_lon}")
    except:
        print("✗ Coordonnées invalides !")
        return False
    
    # Réinitialiser le compteur d'échantillons pour cette nouvelle position
    sample_count = 0
    
    # Afficher la configuration
    print(f"\n Configuration OK")
    print(f"   Salle: {current_room} | Étage: {current_floor}")
    print(f"   Attente de {target_samples} échantillons (~5 minutes)...\n")
    
    return True

# ============================================================================
# PROGRAMME PRINCIPAL
# ============================================================================

if __name__ == '__main__':
    """
    Point d'entrée du script de collecte.
    
    WORKFLOW:
        1. Afficher le header
        2. Initialiser la base SQLite
        3. Se connecter à TTN via MQTT
        4. Demander la première position
        5. Attendre les échantillons (callback on_message)
        6. Permettre de passer à la position suivante
        7. Afficher les statistiques finales
    
    COMMANDES UTILISATEUR:
        - 'next' : Passer à une nouvelle position
        - 'quit' : Arrêter la collecte
        - Ctrl+C : Arrêt d'urgence
    
    STATISTIQUES FINALES:
        - Nombre total de fingerprints collectés
        - Liste des salles enregistrées
    """
    # Header informatif
    print("""
╔═══════════════════════════════════════════════════════════╗
║     COLLECTE AUTOMATIQUE - SALLE 203                      ║
║     5 échantillons = ~5 minutes                          ║
╚═══════════════════════════════════════════════════════════╝
""")
    
    # Initialiser la base de données
    init_db()
    
    # Créer le client MQTT (gérer les versions de l'API paho-mqtt)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except:
        client = mqtt.Client()
    
    # Configurer les identifiants TTN
    # Username: {application_id}@ttn
    # Password: API Key (généré dans TTN Console)
    client.username_pw_set(
        "project1-sniffer@ttn",
        "NNSXS.URZ75UUXP7WCFQJ33P4XTXTL4D4YXK2D2A5P63A.AAKKN5KZOCIFHZ6KA654WBQXXYUOTKUONITP5DEJKMAP2EONXMRQ"
    )
    
    # Définir les callbacks
    client.on_connect = on_connect          # Appelé lors de la connexion
    client.on_message = on_message          # Appelé à chaque message reçu
    
    # Se connecter au broker MQTT de TTN
    # Host: eu1.cloud.thethings.network (serveur européen)
    # Port: 1883 (MQTT non sécurisé, 8883 pour MQTTS)
    # Keepalive: 60 secondes
    client.connect("eu1.cloud.thethings.network", 1883, 60)
    
    # Démarrer la boucle MQTT en arrière-plan (non-bloquant)
    client.loop_start()
    
    # Demander la première position
    if not set_location():
        print("Arrêt - coordonnées invalides")
        exit()
    
    # Boucle interactive principale
    try:
        while True:
            # Attendre une commande utilisateur
            cmd = input("Commande (next/quit): ").strip().lower()
            
            if cmd == 'quit':
                break
            elif cmd == 'next':
                # Configurer une nouvelle position
                if not set_location():
                    break
    
    except KeyboardInterrupt:
        # Gérer Ctrl+C proprement
        print("\n\nArrêt...")
    
    # Arrêter la boucle MQTT et se déconnecter
    client.loop_stop()
    client.disconnect()
    
    print("\n✓ Collecte terminée")
    
    # Afficher les statistiques finales
    conn = sqlite3.connect(DB_PATH)
    
    # Compter le nombre total de fingerprints
    total = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    
    # Lister les salles uniques collectées
    rooms = conn.execute("SELECT DISTINCT room FROM fingerprints").fetchall()
    
    conn.close()
    
    print(f"✓ Total: {total} fingerprints")
    print(f"✓ Salles: {', '.join([r[0] for r in rooms])}")