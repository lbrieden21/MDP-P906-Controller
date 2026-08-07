#ifndef NET_ETH_H
#define NET_ETH_H

/*
 * Teensy-private entry point for Ethernet bring-up. net_eth.cpp owns this
 * plus the platform.h net_creds_save()/net_ip_config_save()/net_status()
 * hooks; nothing else in the tree needs to know HOST_LINK_ETH exists.
 */

/* Brings up QNEthernet from the stored static-IP record (or DHCP) if
   HOST_LINK_ETH is set; a no-op otherwise. Must never block -- Ethernet.begin()
   starts DHCP asynchronously and returns, and nothing may wait for a lease or
   for link. Safe to call unconditionally -- called once from platform_init(). */
void net_eth_begin(void);

#endif
