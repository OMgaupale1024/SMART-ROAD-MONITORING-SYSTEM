/*
 * RoadSense — Arduino UNO firmware (car-in-a-box prototype)
 *
 * Sends a stable, line-based CSV protocol to the PC over USB serial at 115200 baud,
 * while keeping the OLED, LED, and buzzer working locally whether or not a PC is attached.
 *
 * Hardware:
 *   MPU6050 accelerometer  I2C 0x68   (direct Wire reads; NO mpu.testConnection())
 *   HC-SR04 ultrasonic     TRIG=D9  ECHO=D10   (NA on timeout — never faked)
 *   SSD1306 OLED           I2C 0x3C
 *   Status LED             D6
 *   Buzzer                 D7
 *
 * Serial protocol (each line ends with '\n'):
 *   HELLO,ROADSENSE,1                              once at startup
 *   T,<ms>,<ay>,<shock>,<distance_cm|NA>,<status>  telemetry, ~10 Hz
 *   E,<ms>,<ay>,<shock>,<distance_cm|NA>,<status>  on a classified event
 *   status is one of: NORMAL, SPEED_BREAKER, POTHOLE
 *
 * No other debug text is printed on this serial line once running.
 *
 * Requires libraries: Adafruit GFX, Adafruit SSD1306 (install via Library Manager).
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ---------------- Pins ----------------
static const uint8_t PIN_TRIG = 9;
static const uint8_t PIN_ECHO = 10;
static const uint8_t PIN_LED  = 6;
static const uint8_t PIN_BUZZ = 7;

// ---------------- MPU6050 -------------
static const uint8_t MPU_ADDR        = 0x68;
static const uint8_t MPU_PWR_MGMT_1  = 0x6B;
static const uint8_t MPU_ACCEL_XOUT_H = 0x3B; // acceleration registers begin here

// ---------------- OLED ----------------
static const uint8_t OLED_ADDR = 0x3C;
Adafruit_SSD1306 display(128, 64, &Wire, -1);
bool oledReady = false;

// ------------- Tunable knobs (CALIBRATE ON THE VEHICLE) -------------
// shock = abs(AY - previousAY). These placeholder thresholds must be tuned against
// real readings for this specific chassis/mounting before trusting the classification.
static const int SHOCK_SPEED_BREAKER = 8000;
static const int SHOCK_POTHOLE       = 15000;

static const unsigned long TELEMETRY_INTERVAL_MS = 100;  // ~10 Hz
static const unsigned long EVENT_COOLDOWN_MS     = 400;  // min gap between E messages
static const unsigned long ULTRASONIC_TIMEOUT_US = 25000; // ~4 m max range

// ---------------- State ----------------
int16_t previousAY = 0;
unsigned long lastTelemetryMs = 0;
unsigned long lastEventMs = 0;

// Read 6 accel bytes starting at 0x3B and return AY (bytes 3-4). Direct Wire I2C.
void readAccel(int16_t &ax, int16_t &ay, int16_t &az) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(MPU_ACCEL_XOUT_H);
  Wire.endTransmission(false);
  Wire.requestFrom((int)MPU_ADDR, 6, (int)true);
  if (Wire.available() < 6) { ax = 0; ay = previousAY; az = 0; return; }
  ax = (int16_t)((Wire.read() << 8) | Wire.read());
  ay = (int16_t)((Wire.read() << 8) | Wire.read());
  az = (int16_t)((Wire.read() << 8) | Wire.read());
}

// Return distance in cm, or -1 when the echo times out (reported to the PC as NA).
long readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  unsigned long duration = pulseIn(PIN_ECHO, HIGH, ULTRASONIC_TIMEOUT_US);
  if (duration == 0) return -1;            // unavailable — do NOT fabricate a value
  return (long)(duration * 0.0343 / 2.0);
}

const char* classify(int shock) {
  if (shock >= SHOCK_POTHOLE)       return "POTHOLE";
  if (shock >= SHOCK_SPEED_BREAKER) return "SPEED_BREAKER";
  return "NORMAL";
}

void updateOled(int16_t ay, int shock, long distance, const char* status) {
  if (!oledReady) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("RoadSense");
  display.setTextSize(2);
  display.setCursor(0, 14);
  display.println(status);
  display.setTextSize(1);
  display.setCursor(0, 40);
  display.print("AY:"); display.print(ay);
  display.print(" S:"); display.println(shock);
  display.setCursor(0, 52);
  display.print("Dist:");
  if (distance < 0) display.println("NA"); else { display.print(distance); display.println("cm"); }
  display.display();
}

// Print one protocol line: <kind>,<ms>,<ay>,<shock>,<dist|NA>,<status>
void sendMessage(char kind, unsigned long ms, int16_t ay, int shock, long distance, const char* status) {
  Serial.print(kind);   Serial.print(',');
  Serial.print(ms);     Serial.print(',');
  Serial.print(ay);     Serial.print(',');
  Serial.print(shock);  Serial.print(',');
  if (distance < 0) Serial.print("NA"); else Serial.print(distance);
  Serial.print(',');    Serial.println(status);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BUZZ, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  Wire.begin();
  // Wake the MPU6050 (clear sleep bit). Direct register write — no library handshake.
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(MPU_PWR_MGMT_1);
  Wire.write(0x00);
  Wire.endTransmission(true);

  oledReady = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  if (oledReady) { display.clearDisplay(); display.display(); }

  int16_t ax, ay, az;
  readAccel(ax, ay, az);
  previousAY = ay;                 // seed so the first shock isn't a huge spike

  Serial.println("HELLO,ROADSENSE,1");
}

void loop() {
  unsigned long now = millis();
  if (now - lastTelemetryMs < TELEMETRY_INTERVAL_MS) return;
  lastTelemetryMs = now;

  int16_t ax, ay, az;
  readAccel(ax, ay, az);
  int shock = abs((int)ay - (int)previousAY);
  previousAY = ay;

  long distance = readDistanceCm();
  const char* status = classify(shock);
  bool alert = (status[0] != 'N');   // anything other than NORMAL

  // Local feedback — runs regardless of any PC connection.
  digitalWrite(PIN_LED, alert ? HIGH : LOW);
  if (alert) tone(PIN_BUZZ, 2000, 60);
  updateOled(ay, shock, distance, status);

  // Telemetry to the PC.
  sendMessage('T', now, ay, shock, distance, status);

  // Classified event (rate-limited so a sustained bump isn't spammed).
  if (alert && (now - lastEventMs >= EVENT_COOLDOWN_MS)) {
    lastEventMs = now;
    sendMessage('E', now, ay, shock, distance, status);
  }
}
