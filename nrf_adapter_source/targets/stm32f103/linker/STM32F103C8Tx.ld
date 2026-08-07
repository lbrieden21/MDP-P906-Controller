/*
 * STM32F103C8Tx (Blue Pill): 64KB flash, 20KB SRAM. Last 1KB flash page is
 * reserved for settings storage (see flash_store.c) and excluded from the
 * code region so the linker never places .text/.rodata/.data-init there.
 * F103C8 is medium-density, so the erase page size is 1K -- the same
 * last-page-reserved scheme from the F030 target transfers exactly.
 */
MEMORY
{
  FLASH (rx)         : ORIGIN = 0x08000000, LENGTH = 63K
  SETTINGS_FLASH (r) : ORIGIN = 0x0800FC00, LENGTH = 1K
  RAM (rwx)          : ORIGIN = 0x20000000, LENGTH = 20K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);
_settings_flash_page = ORIGIN(SETTINGS_FLASH);

SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH

  .text :
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } >FLASH

  _sidata = LOADADDR(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } >RAM

  ._user_heap_stack (NOLOAD) :
  {
    . = ALIGN(8);
    . = . + 0x200; /* minimum stack headroom check */
    . = ALIGN(8);
  } >RAM

  /DISCARD/ :
  {
    *(.ARM.exidx*)
  }
}
