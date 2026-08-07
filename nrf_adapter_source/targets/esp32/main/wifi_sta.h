#ifndef WIFI_STA_H
#define WIFI_STA_H

/*
 * ESP32-private entry point for WiFi station bring-up. wifi_sta.c owns this
 * plus the platform.h net_creds_save()/net_ip_config_save()/net_status()
 * hooks; nothing else in the tree needs to know CONFIG_HOST_LINK_WIFI exists.
 */

/* Brings up the WiFi station and its reconnect logic if CONFIG_HOST_LINK_WIFI
   is set and credentials are stored in NVS; a no-op otherwise. Safe to call
   unconditionally -- called once from platform_init(). */
void wifi_sta_init(void);

#endif
