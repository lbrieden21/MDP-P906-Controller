/*
 * WiFi station bring-up, reconnect and credential storage. Owns the
 * platform.h wifi_creds_save()/wifi_status() hooks for the ESP32 target.
 *
 * Gated on CONFIG_HOST_LINK_WIFI internally rather than by CMakeLists SRCS,
 * so every ESP32 build links this file and gets a real wifi_creds_save()/
 * wifi_status() either way -- a non-WiFi build just gets the stub at the
 * bottom, on the same footing as the four non-ESP32 targets' stubs.
 * CONFIG_HOST_LINK_WIFI is what keeps the 2.4GHz radio off in every build
 * that doesn't ask for it (decision 6 in the plan doc): wifi_sta_init() is a
 * no-op there, so nothing calls esp_wifi_start() and the coexistence
 * question this feature raises simply does not arise for that build.
 *
 * Credentials get their own NVS key ("wifi" in the "p906" namespace),
 * deliberately not folded into the persisted_settings_t blob protocol.c
 * already owns -- growing that struct would silently invalidate every saved
 * radio setting on every board already in the field (store_load() checks the
 * stored blob size for exact equality). esp_wifi_set_storage(WIFI_STORAGE_RAM)
 * keeps IDF's own WiFi-credential NVS copy out of the picture entirely, so
 * this key is the only place credentials live and CMD_WIFI_CLEAR has exactly
 * one thing to erase.
 */

#include "sdkconfig.h"

#if CONFIG_HOST_LINK_WIFI

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"
#include "nvs.h"

#include "platform.h"
#include "wifi_sta.h"

static const char *TAG = "wifi_sta";

#define NVS_NS "p906"
#define NVS_KEY "wifi"

typedef struct {
    char ssid[33];
    char pass[65];
} wifi_creds_t;

/* REP_WIFI_STATUS's state byte -- see core/protocol.h. Updated only from the
   default event-loop task (event_handler() below), read from protocol_poll()
   on the main loop task; a plain byte read/write needs no lock, same as
   radio_irq_flag in platform_esp32.c. */
enum { STA_DISCONNECTED = 0, STA_CONNECTING = 1, STA_CONNECTED = 2 };
static volatile uint8_t s_state = STA_DISCONNECTED;
static esp_netif_t *s_netif;
static bool s_have_creds;

/* esp_wifi_start() is asynchronous -- it returns before the WiFi driver task
   has actually processed the start, so esp_wifi_connect() called right after
   it (as the boot path does, via apply_creds() from wifi_sta_init()) races
   the driver and can be silently dropped: no connection is attempted and no
   event is ever posted, which is indistinguishable from a permanent hang
   since nothing arrives to trigger schedule_reconnect(). ESP-IDF's own
   station example (examples/wifi/getting_started/station) only ever calls
   esp_wifi_connect() from inside the WIFI_EVENT_STA_START handler for this
   reason. The live CMD_WIFI_SET path never hits this race -- STA has already
   been running for the whole session by the time a user provisions -- which
   is why only the boot-time reconnect was ever seen to hang. */
static bool s_sta_started;

/* Reconnect backoff: doubles on each consecutive disconnect, resets to the
   base the moment an association succeeds. This is minutes-scale link
   recovery against a flaky AP, not the ~40ms per-request host timeout --
   the goal is not hammering the AP, not fast failover. */
#define RECONNECT_BASE_MS 1000
#define RECONNECT_MAX_MS 30000
static TimerHandle_t s_reconnect_timer;
static uint32_t s_reconnect_delay_ms = RECONNECT_BASE_MS;

static int nvs_creds_load(wifi_creds_t *out) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return 0;
    }
    size_t stored = 0;
    if (nvs_get_blob(h, NVS_KEY, NULL, &stored) != ESP_OK || stored != sizeof(*out)) {
        nvs_close(h);
        return 0;
    }
    esp_err_t err = nvs_get_blob(h, NVS_KEY, out, &stored);
    nvs_close(h);
    return err == ESP_OK ? 1 : 0;
}

static int nvs_creds_store(const wifi_creds_t *in) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return 0;
    }
    if (nvs_set_blob(h, NVS_KEY, in, sizeof(*in)) != ESP_OK || nvs_commit(h) != ESP_OK) {
        nvs_close(h);
        return 0;
    }
    nvs_close(h);
    return 1;
}

static int nvs_creds_erase(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return 0;
    }
    esp_err_t err = nvs_erase_key(h, NVS_KEY);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(h);
        return 0;
    }
    err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK ? 1 : 0;
}

static void reconnect_timer_cb(TimerHandle_t timer) {
    (void)timer;
    if (s_have_creds) {
        esp_wifi_connect();
    }
}

static void schedule_reconnect(void) {
    xTimerChangePeriod(s_reconnect_timer, pdMS_TO_TICKS(s_reconnect_delay_ms), 0);
    xTimerStart(s_reconnect_timer, 0);
    s_reconnect_delay_ms *= 2;
    if (s_reconnect_delay_ms > RECONNECT_MAX_MS) {
        s_reconnect_delay_ms = RECONNECT_MAX_MS;
    }
}

static void event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        s_sta_started = true;
        if (s_have_creds) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_state = STA_DISCONNECTED;
        if (s_have_creds) {
            schedule_reconnect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_state = STA_CONNECTED;
        s_reconnect_delay_ms = RECONNECT_BASE_MS;
        ESP_LOGI(TAG, "connected");
    }
}

/* Persists nothing; nvs_creds_store()/nvs_creds_erase() do that. This is the
   "apply live" half, so a provisioning command connects without a reboot --
   esp_wifi_disconnect() is harmless if already disconnected and makes a
   mid-session credential change take immediately rather than racing the old
   association. */
static void apply_creds(const wifi_creds_t *creds) {
    /* wifi_config_t is a union of ap/sta/nan configs of different sizes, and
       .sta itself nests nan-trivial structs deep enough that an aggregate
       `= {0}` initializer trips -Werror=missing-braces. memset sidesteps
       both that and the union-zero-init subtlety (`= {0}` only guarantees
       the first named member, .ap, is zeroed, not the possibly-larger .sta). */
    wifi_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    memcpy(cfg.sta.ssid, creds->ssid, sizeof(cfg.sta.ssid));
    memcpy(cfg.sta.password, creds->pass, sizeof(cfg.sta.password));
    /* Permits WPA2 and above (so the WPA3-SAE bench AP qualifies) rather than
       pinning to one auth mode; PWE_BOTH covers both SAE handshake variants,
       matching the default IDF's own docs recommend. */
    cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    s_have_creds = true;
    s_state = STA_CONNECTING;
    s_reconnect_delay_ms = RECONNECT_BASE_MS;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    esp_wifi_disconnect();
    /* If STA hasn't actually started yet (the boot-time race described above
       the s_sta_started declaration), don't connect here -- the
       WIFI_EVENT_STA_START handler will, once it's safe to. */
    if (s_sta_started) {
        esp_wifi_connect();
    }
}

void wifi_sta_init(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    /* Keep IDF's own WiFi-credential NVS copy out of the picture -- see the
       file header comment. Must be set after esp_wifi_init(), before
       esp_wifi_start(). */
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                         &event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                         &event_handler, NULL, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Phase 0 measured no consistent latency win from WIFI_PS_NONE, but it is
       a one-line, zero-cost setting IDF's own docs still recommend for a
       latency-sensitive link -- see the plan doc's Phase 0 section. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    s_reconnect_timer = xTimerCreate("wifi_reconnect", pdMS_TO_TICKS(RECONNECT_BASE_MS),
                                      pdFALSE, NULL, reconnect_timer_cb);

    wifi_creds_t creds;
    if (nvs_creds_load(&creds)) {
        ESP_LOGI(TAG, "found stored credentials, connecting");
        apply_creds(&creds);
    } else {
        ESP_LOGI(TAG, "no stored credentials -- radio started, waiting to be provisioned");
    }
}

int wifi_creds_save(const char *ssid, const char *pass) {
    if (ssid == NULL) {
        s_have_creds = false;
        s_state = STA_DISCONNECTED;
        esp_wifi_disconnect();
        return nvs_creds_erase();
    }

    wifi_creds_t creds = {0};
    strncpy(creds.ssid, ssid, sizeof(creds.ssid) - 1);
    strncpy(creds.pass, pass, sizeof(creds.pass) - 1);

    if (!nvs_creds_store(&creds)) {
        return 0;
    }
    apply_creds(&creds);
    return 1;
}

int wifi_status(uint8_t *state, uint8_t ip[4], int8_t *rssi, char ssid[33]) {
    *state = s_state;
    ip[0] = ip[1] = ip[2] = ip[3] = 0;
    *rssi = 0;
    ssid[0] = '\0';

    if (s_state != STA_CONNECTED) {
        return 1;
    }

    wifi_ap_record_t ap_info;
    if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
        *rssi = (int8_t)ap_info.rssi;
        memcpy(ssid, ap_info.ssid, sizeof(ap_info.ssid)); /* uint8_t ssid[33], NUL-terminated by IDF */
        ssid[32] = '\0';
    }

    esp_netif_ip_info_t ip_info;
    if (esp_netif_get_ip_info(s_netif, &ip_info) == ESP_OK) {
        uint32_t addr = ip_info.ip.addr;
        ip[0] = (uint8_t)(addr & 0xFF);
        ip[1] = (uint8_t)((addr >> 8) & 0xFF);
        ip[2] = (uint8_t)((addr >> 16) & 0xFF);
        ip[3] = (uint8_t)((addr >> 24) & 0xFF);
    }
    return 1;
}

#else /* !CONFIG_HOST_LINK_WIFI */

#include "platform.h"
#include "wifi_sta.h"

void wifi_sta_init(void) {}

int wifi_creds_save(const char *ssid, const char *pass) {
    (void)ssid;
    (void)pass;
    return 0;
}

int wifi_status(uint8_t *state, uint8_t ip[4], int8_t *rssi, char ssid[33]) {
    (void)state;
    (void)ip;
    (void)rssi;
    (void)ssid;
    return 0;
}

#endif
