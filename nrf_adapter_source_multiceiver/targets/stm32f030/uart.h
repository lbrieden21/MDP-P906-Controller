#ifndef UART_H
#define UART_H

#include <stdint.h>

/* Bring-up only. uart_write()/uart_read_byte()/uart_set_baudrate() are
   declared in platform.h. */
void uart_init(uint32_t baudrate);

#endif
