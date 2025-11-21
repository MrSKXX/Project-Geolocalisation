#include "WiFi.h"
#include "HardwareSerial.h"

#define NETWORKS_TO_SEND 3
#define LED_PIN 2

String DEVEUI = "70B3D57ED0074147";
String APPEUI = "0000000000000000";
String APPKEY = "92C305A789728E04C6861E600FF05424";

HardwareSerial LoRaSerial(2);
unsigned long lastSendTime = 0;
const unsigned long sendInterval = 45000;

bool isValidSSID(String ssid) {
  ssid.toUpperCase();
  if (ssid.indexOf("IPHONE") >= 0) return false;
  if (ssid.indexOf("ANDROID") >= 0) return false;
  if (ssid.length() == 0) return false;
  return true;
}

void sendATCommand(const String& cmd) {
  Serial.println(">>> " + cmd);
  LoRaSerial.println(cmd);
  delay(1000);
  while (LoRaSerial.available()) {
    String line = LoRaSerial.readString();
    Serial.print("<<< ");
    Serial.print(line);
  }
}

void setup() {
  Serial.begin(115200);
  LoRaSerial.begin(9600, SERIAL_8N1, 16, 17);
  LoRaSerial.setTimeout(2000);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.println("Initialisation du WiFi en mode STA (pour le scan)...");
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  delay(2000);
  Serial.println("--- Configuring LoRa-E5 Module ---");

  sendATCommand("AT");
  sendATCommand("AT+ID=DevEUI," + DEVEUI);
  sendATCommand("AT+ID=AppEUI," + APPEUI);
  sendATCommand("AT+KEY=APPKEY," + APPKEY);
  sendATCommand("AT+DR=EU868");
  sendATCommand("AT+MODE=LWOTAA");
  sendATCommand("AT+DR=DR5");

  Serial.println("--- Attempting to Join LoRaWAN Network ---");
  sendATCommand("AT+JOIN");
  delay(10000);

  Serial.println("--- Configuration Complete ---");
}

void loop() {
  if (millis() - lastSendTime >= sendInterval) {
    lastSendTime = millis();
    digitalWrite(LED_PIN, HIGH);
    sendWiFiScanData();
    digitalWrite(LED_PIN, LOW);
  }
  checkDownlink();
}

void sendWiFiScanData() {
  Serial.println("Lancement du scan WiFi...");
  int n = WiFi.scanNetworks();

  if (n == 0) {
    Serial.println("Aucun réseau trouvé. Envoi annulé.");
    return;
  }
  Serial.print(n);
  Serial.println(" réseaux trouvés.");

  int top_indices[NETWORKS_TO_SEND];
  int top_rssi[NETWORKS_TO_SEND];

  for (int i = 0; i < NETWORKS_TO_SEND; i++) {
    top_indices[i] = -1;
    top_rssi[i] = -100;
  }

  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    if (!isValidSSID(ssid)) {
      Serial.println("  Filtré: " + ssid);
      continue;
    }

    int rssi = WiFi.RSSI(i);

    for (int k = 0; k < NETWORKS_TO_SEND; k++) {
      if (rssi > top_rssi[k]) {
        for (int j = NETWORKS_TO_SEND - 1; j > k; j--) {
          top_rssi[j] = top_rssi[j - 1];
          top_indices[j] = top_indices[j - 1];
        }
        top_rssi[k] = rssi;
        top_indices[k] = i;
        break;
      }
    }
  }

  char payload_buffer[(NETWORKS_TO_SEND * 7 * 2) + 1];
  char* buf_ptr = payload_buffer;

  Serial.println("Préparation de la payload LoRaWAN :");

  for (int i = 0; i < NETWORKS_TO_SEND; i++) {
    int index = top_indices[i];

    if (index != -1) {
      uint8_t* bssid = WiFi.BSSID(index);
      int8_t rssi = (int8_t)WiFi.RSSI(index);

      buf_ptr += sprintf(buf_ptr, "%02X%02X%02X%02X%02X%02X",
                         bssid[0], bssid[1], bssid[2], bssid[3], bssid[4], bssid[5]);
      buf_ptr += sprintf(buf_ptr, "%02X", (uint8_t)rssi);

      Serial.print("  Slot ");
      Serial.print(i + 1);
      Serial.print(": ");
      Serial.print(WiFi.BSSIDstr(index));
      Serial.print(" / RSSI: ");
      Serial.println(rssi);

    } else {
      buf_ptr += sprintf(buf_ptr, "00000000000000");
      Serial.print("  Slot ");
      Serial.print(i + 1);
      Serial.println(": Vide (padding)");
    }
  }

  *buf_ptr = '\0';
  String payload(payload_buffer);

  Serial.println("Payload Hex: " + payload);

  sendATCommand("AT+CMSGHEX=" + payload);
}

void checkDownlink() {
  while (LoRaSerial.available()) {
    String line = LoRaSerial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    Serial.println("<<< " + line);

    if (line.indexOf("+CMSGHEX:") != -1 && line.indexOf("RX:") != -1) {
      Serial.println("Downlink reçu !");
    }
  }
}