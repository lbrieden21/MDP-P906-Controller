#ifndef UART_H
#define UART_H

#include <stddef.h>
#include <stdint.h>

void uart1_init(uint32_t baudrate);
void uart1_set_baudrate(uint32_t baudrate);
void uart1_write(const uint8_t *data, size_t len);
/* Pops one buffered RX byte. Returns 1 and fills *out if one was available, else 0. */
int uart1_read_byte(uint8_t *out);

#endif
