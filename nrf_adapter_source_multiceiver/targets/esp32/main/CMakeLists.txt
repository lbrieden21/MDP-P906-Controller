# core/ is compiled straight into the main component rather than being made a
# component of its own: it is four files with no Kconfig and no dependencies
# beyond platform.h, and a component wrapper would need its own CMakeLists in
# a shared directory that the ARM targets also build from.
# Host link select. Exactly one implementation is compiled, along with the IDF
# driver component it needs -- following the F103's HOST_LINK precedent, where
# usb_cdc.c and uart.c are also separate files chosen by the build rather than
# #ifdef'd against each other.
#
# SRCS is keyed off the Kconfig choice in Kconfig.projbuild. The Makefile owns
# which fragment sets it, the per-board default, and the rejection of the
# impossible combinations (classic ESP32 has no USB-Serial/JTAG; UART0 cannot be
# both the protocol link and the console).
if(CONFIG_HOST_LINK_UART0)
    set(HOST_LINK_SRC "host_link_uart0.c")
else()
    # Deliberately an else() and not an elseif(CONFIG_HOST_LINK_USB_JTAG) with a
    # FATAL_ERROR fallback: this file is also evaluated by the early requirements
    # pass described below, where no sdkconfig exists yet and every CONFIG_* is
    # empty, so any "neither was selected" error would fire there every build.
    # In the main pass a Kconfig choice always resolves to exactly one member, so
    # there is no third case to guard against.
    set(HOST_LINK_SRC "host_link_usb_jtag.c")
endif()

# REQUIRES, however, CANNOT be made conditional on any of that, and the reason is
# worth stating so nobody tries again:
#
# IDF expands component requirements in an early pass that runs as a separate
# CMake process (component.cmake's __component_get_requirements ->
# scripts/component_get_requirements.cmake), and build.cmake calls it at :665,
# BEFORE __kconfig_generate_config at :705. So during that pass there is no
# sdkconfig yet -- every CONFIG_* is empty -- and the child process does not
# inherit the parent's cache either, so a -D variable is equally invisible.
# Either one selects SRCS correctly in the main pass and leaves REQUIRES silently
# wrong, which surfaces as a "component is not in the requirements list" error at
# compile time rather than at configure time.
#
# So both drivers are required unconditionally. That costs nothing: the component
# not in use contributes no code that survives --gc-sections, and on classic
# ESP32 esp_driver_usb_serial_jtag registers with an empty source list anyway
# (its CMakeLists gates every source on CONFIG_SOC_USB_SERIAL_JTAG_SUPPORTED),
# so requiring it there is legal and empty. Verified by build size: naming both
# leaves every configuration's image byte-identical to naming only the one it
# uses.
#
# esp_wifi/esp_netif/esp_event/lwip join this list for the same reason and are
# equally unconditional -- CONFIG_HOST_LINK_WIFI does not exist yet during the
# early requirements pass either. Verified the same way (Phase 0 of the plan
# doc): `make BOARD=ESP32C6 size` produced a byte-identical non-WiFi image
# before and after adding these four, even though project_description.json
# confirmed all of them (plus wifi_provisioning, pulled in transitively) were
# genuinely part of the build graph.
set(HOST_LINK_REQUIRES esp_driver_usb_serial_jtag esp_driver_uart)

idf_component_register(
    SRCS
        "main.c"
        "platform_esp32.c"
        "host_link_mux.c"
        "host_link_wifi.c"
        "wifi_sta.c"
        "${HOST_LINK_SRC}"
        "../../../core/protocol.c"
        "../../../core/nrf24l01p.c"
    INCLUDE_DIRS
        "."
        "../../.."      # platform.h
        "../../../core" # protocol.h, nrf24l01p.h
    REQUIRES
        ${HOST_LINK_REQUIRES}
        esp_driver_spi
        esp_driver_gpio
        esp_timer
        nvs_flash
        esp_system
        esp_wifi
        esp_netif
        esp_event
        lwip
)

# Matches the -Wall -Wextra every other target's Makefile gives core/ and the
# platform layer. The vendored-framework carve-out the Teensy targets need has
# no analogue here: ESP-IDF is built by its own component CMakeLists with its
# own warning settings, so this only ever reaches our four files.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
