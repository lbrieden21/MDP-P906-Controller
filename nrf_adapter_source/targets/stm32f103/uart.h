#ifndef UART_H
#define UART_H

#include <stdint.h>

/* Bring-up only. host_link_write()/host_link_read_byte()/host_link_set_baudrate() are
   declared in platform.h. */
void uart_init(uint32_t baudrate);

#endif
