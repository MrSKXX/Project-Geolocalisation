#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// --- CONFIGURATION ---
const char* HOTSPOT_SSID = "XUltranetX";
const char* HOTSPOT_PASS = "$x1222002$";

// IP corrigée selon tes logs (Gateway)
const char* SERVER_URL = "http://10.88.222.214:8000/api/update-http";

const char* BLACKLIST_KEYWORDS[] = {
    "iPhone", "Android", "Galaxy", "Pixel", "Huawei", "Xiaomi"
};
const int BLACKLIST_COUNT = 6;

const char* ALLOWED_SSIDS[] = {
    "eduroam", "eduspot", "UPMC", "Polytech", "SCAI", "CONGRES"
};
const int ALLOWED_COUNT = 6;

String scannerId;

// --- FONCTIONS DE FILTRAGE ---

bool isBlacklistedSSID(String ssid) {
    if (ssid.length() == 0) return true;
    if (ssid.equalsIgnoreCase(HOTSPOT_SSID)) return true;
    
    String s = ssid;
    s.toUpperCase();
    
    for (int i = 0; i < BLACKLIST_COUNT; i++) {
        String k = String(BLACKLIST_KEYWORDS[i]);
        k.toUpperCase();
        if (s.indexOf(k) >= 0) return true;
    }
    return false;
}

bool isAllowedSSID(String ssid) {
    for (int i = 0; i < ALLOWED_COUNT; i++) {
        if (ssid.equalsIgnoreCase(ALLOWED_SSIDS[i])) return true;
    }
    return false;
}

// --- SETUP ---

void setup() {
    Serial.begin(115200);
    WiFi.mode(WIFI_STA);

    // Identifiant unique du scanner
    scannerId = WiFi.macAddress();
    
    Serial.println("\n--- DEMARRAGE ---");
    Serial.print("ID Scanner: ");
    Serial.println(scannerId);
    
    Serial.print("Connexion a ");
    Serial.println(HOTSPOT_SSID);
    
    WiFi.begin(HOTSPOT_SSID, HOTSPOT_PASS);
    
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    
    Serial.println("\nConnecte.");
    Serial.print("IP ESP32: ");
    Serial.println(WiFi.localIP());
    Serial.print("Gateway (Serveur): ");
    Serial.println(WiFi.gatewayIP());
    Serial.print("URL Cible: ");
    Serial.println(SERVER_URL);
}

// --- LOOP ---

void loop() {
    // 1. Verification WiFi
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Connexion perdue. Tentative de reconnexion...");
        WiFi.disconnect();
        WiFi.reconnect();
        delay(5000);
        return;
    }

    // 2. Scan
    Serial.println("\n--- DEBUT SCAN ---");
    int n = WiFi.scanNetworks();
    Serial.printf("Reseaux trouves: %d\n", n);
    
    DynamicJsonDocument doc(10240);
    doc["scanner_id"] = scannerId;
    JsonArray nets = doc.createNestedArray("networks");
    
    int kept = 0;
    
    for (int i = 0; i < n; i++) {
        String ssid = WiFi.SSID(i);
        String mac = WiFi.BSSIDstr(i);
        int rssi = WiFi.RSSI(i);
        int channel = WiFi.channel(i);
        
        Serial.printf("[%d] %-20s | %d dBm ", i, ssid.c_str(), rssi);
        
        // Logique de filtrage avec affichage
        if (isBlacklistedSSID(ssid)) {
            Serial.println("-> REJETE (Blacklist)");
            continue;
        }
        
        if (!isAllowedSSID(ssid)) {
            Serial.println("-> REJETE (Nom inconnu)");
            continue;
        }
        
        // Si on arrive ici, le reseau est valide
        Serial.println("-> GARDE");
        
        JsonObject net = nets.createNestedObject();
        net["ssid"] = ssid;
        net["mac"] = mac;
        net["rssi"] = rssi;
        net["channel"] = channel;
        kept++;
        
        if (kept >= 20) break; // Limite pour eviter surcharge
    }
    
    Serial.printf("Total envoye: %d reseaux\n", kept);
    
    // 3. Envoi HTTP
    if (kept > 0) {
        WiFiClient client;
        HTTPClient http;
        
        if (http.begin(client, SERVER_URL)) {
            http.addHeader("Content-Type", "application/json");
            http.setTimeout(5000);
            
            String json;
            serializeJson(doc, json);
            
            int code = http.POST(json);
            
            if (code > 0) {
                Serial.printf("HTTP Code: %d\n", code);
                if (code != 200) {
                    Serial.print("Reponse: ");
                    Serial.println(http.getString());
                }
            } else {
                Serial.printf("Erreur HTTP: %s\n", http.errorToString(code).c_str());
            }
            http.end();
        } else {
            Serial.println("Erreur: Impossible d'initialiser HTTP");
        }
    }
    
    WiFi.scanDelete();
    delay(2000); 
}