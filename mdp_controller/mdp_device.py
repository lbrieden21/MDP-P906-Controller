import time
from threading import Event
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal as mdp_protocal
from mdp_controller.nrf24_adapter import NRF24AdapterError

if TYPE_CHECKING:
    from mdp_controller.bus import MDPBus


def _convert_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _hex_to_bytes(s: str) -> bytes:
    s = s.replace("0x", "").replace(":", "").replace(" ", "")
    return bytes.fromhex(s)


class MDPDevice:
    """
    Shared transport layer for every MDP device driver on an MDPBus.

    Holds the radio-side plumbing common to all devices -- construction
    parameters, the per-instance transfer handshake state, packet dispatch,
    and the connect retry loop -- so a concrete driver only has to supply the
    packet types that are actually device-specific.

    Bus-side contract (all of this is written/read by MDPBus, not by the
    device itself -- see bus.py):
        - MDPBus.attach() writes ``.address`` (the device's RX pipe address)
          and registers the device in the bus's pipe-owner table.
        - MDPBus.transfer() reads ``.com_retry`` / ``.com_timeout``, then for
          a waited send clears ``_transfer_event``, sets ``_transfer_data``
          to b"" and ``_transfer_wait_header`` to the request's type byte,
          and waits on ``_transfer_event`` for the reply.
        - MDPBus._on_recv() calls ``_on_packet(data)`` for the owning pipe;
          the tail of _on_packet() is what fills ``_transfer_data`` and sets
          ``_transfer_event`` to release that wait.

    Subclasses must set ``device_name``, assign ``self._status`` after
    ``super().__init__()``, and implement ``get_status()``,
    ``_handle_packet()`` and ``_connect_probe()``.
    """

    device_name = "MDP-Device"

    #: Message used when connect()'s post-probe readiness check keeps failing.
    _connect_ready_error = "device did not report ready"

    def __init__(
        self,
        bus: "MDPBus",
        idcode: Optional[str] = None,
        m01_channel: int = 0,
        led_color: Tuple[int, int, int] = (0x66, 0xCC, 0xFF),
        com_timeout: Optional[float] = 0.04,
        com_retry: int = 5,
        blink: bool = True,
        debug: bool = False,
    ):
        """
        Args:
            bus (MDPBus): The shared transport this device attaches to (see bus.attach()).
            idcode (Optional[str]): ID code of the device, set to None then call bus.auto_match() to get idcode.
            m01_channel (int): Simulate the MDP-M01, this number shows on top-right of the device's LCD.
            led_color (Tuple[int, int, int]): Color of the digital wheel of the device, in RGB format.
            com_timeout (Optional[float]): Communication timeout in seconds between the device and the adapter.
            com_retry (int): Communication retry times when timeout occurs.
            blink (bool): Whether to blink the "under-control" indicator of the device.
            debug (bool): Show debug info.
        """
        self._bus = bus
        self.address: Optional[bytes] = None
        self._idcode = _hex_to_bytes(idcode) if idcode is not None else None
        self._m01_channel = m01_channel
        self._led_color = _convert_to_rgb565(*led_color)
        self._com_timeout = com_timeout
        self._com_retry = com_retry
        self._blink = blink
        self._debug = debug

        self._transfer_data = b""
        self._transfer_wait_header = -1
        self._transfer_event = Event()

        self._rtvalue_callback: Optional[Callable[[list], None]] = None
        self._status_callback: Optional[Callable[[tuple], None]] = None

    @property
    def idcode(self) -> Optional[bytes]:
        return self._idcode

    @property
    def com_timeout(self) -> Optional[float]:
        return self._com_timeout

    @property
    def com_retry(self) -> int:
        return self._com_retry

    @property
    def speed_counter(self):
        return self._bus.speed_counter

    def _on_packet(self, data: bytes):
        try:
            if not self._handle_packet(data):
                if data[0] == 5:
                    pass
                elif data[0] == 6:
                    logger.info(
                        f"Dispatch device result: {mdp_protocal.parse_type6_response(data)}"
                    )
                else:
                    logger.warning(f"Unhandled Type-{data[0]}: {data.hex(' ').upper()}")

            if self._debug:
                logger.trace(
                    f"Type-{data[0]}: {data.hex(' ').upper()} -> {self._status}"
                )

        except Exception:
            logger.exception("Parse error")

        # Outside the try on purpose: a parse error must still unblock a
        # waiting transfer(), otherwise a malformed packet turns into a hung
        # com_timeout wait.
        if data[0] == self._transfer_wait_header:
            self._transfer_data = data
            self._transfer_wait_header = -1
            self._transfer_event.set()

    def _handle_packet(self, data: bytes) -> bool:
        """Consume a device-specific response type. Return True if handled."""
        raise NotImplementedError

    def _transfer(self, packet: bytes, wait_response: bool = True):
        return self._bus.transfer(self, packet, wait_response)

    def close(self):
        self._bus.detach(self)
        logger.info(f"{self.device_name} closed")

    def get_status(self) -> tuple:
        """
        Get the status of the device.

        Deliberately not implemented here: the return tuple is
        device-specific in both arity meaning and field order, so every
        subclass must define its own. The shared connect() path calls this,
        so it is required, not optional.
        """
        raise NotImplementedError

    def get_realtime_value(self) -> List[Tuple[float, float]]:
        """
        Get the realtime values of output in sync mode.

        Returns:
            List[Tuple[float, float]]: A 9-value list of (voltage/V, current/A)

        Note:
            return [] if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal.gen_get_type8(
                    self._idcode, self._m01_channel, blink=self._blink
                )
            )
            return self._status["RealtimeOutput9"]
        except (TimeoutError, NRF24AdapterError):
            return []

    def request_realtime_value(self) -> bool:
        """
        Request the realtime values of output in async mode.

        Note:
            Should call register_realtime_value_callback() first.

        Returns:
            bool: True if success, False if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal.gen_get_type8(
                    self._idcode, self._m01_channel, blink=self._blink
                ),
                wait_response=False,
            )
            return True
        except (TimeoutError, NRF24AdapterError):
            return False

    def register_realtime_value_callback(self, callback: Callable[[list], None]):
        """
        Register a callback function to handle the realtime values of output in async mode.

        Args:
            callback (Callable[[list], None]): A function that takes a list of (voltage in V, current in A) as input.

        Note:
            The callback will be called in a separate thread. get_realtime_value() will also trigger the callback like request_realtime_value(), but in blocking mode.
        """
        self._rtvalue_callback = callback

    def request_status(self) -> bool:
        """
        Request the status packet (Type-7) in async mode.

        Note:
            Should call register_status_callback() first.

        Returns:
            bool: True if success, False if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal.gen_get_type7(
                    self._idcode, self._m01_channel, blink=self._blink
                ),
                wait_response=False,
            )
            return True
        except (TimeoutError, NRF24AdapterError):
            return False

    def register_status_callback(self, callback: Callable[[tuple], None]):
        """
        Register a callback function to handle the status packet in async mode.

        Args:
            callback (Callable[[tuple], None]): A function that takes the same
                tuple get_status() returns.

        Note:
            The callback will be called in a separate thread, on every
            Type-7 response the device parses -- both from request_status()
            and from get_status()'s own blocking call.
        """
        self._status_callback = callback

    def set_led_color(self, rgb: Tuple[int, int, int]):
        """
        Set the LED color of the digital wheel.

        Args:
            rgb (Tuple[int, int, int]): The color of the LED, in the form of (R, G, B).
        """
        assert self._idcode is not None, "Please pair first"
        rgb565 = _convert_to_rgb565(*rgb)
        self._led_color = rgb565
        logger.debug(f"Set LED color to: {rgb565}")
        self._transfer(
            mdp_protocal.gen_set_led_color(
                self._idcode, self._led_color, self._m01_channel, blink=self._blink
            )
        )

    def connect(self, timeout: float = 8.0):
        """
        Connect to the device.

        Args:
            timeout (float): Total retry budget in seconds. Time-based, not
                attempt-based: a freshly powered-on device ACKs nothing at
                the radio level for the first ~3-4.5 s (measured on real
                hardware for both P906 and L1060), so the budget must
                outlast that boot window for a connect racing a power-on.

        Raises:
            Exception: If failed to connect to the device within the budget.
        """
        assert self._idcode is not None, "Please pair first"
        deadline = time.monotonic() + timeout
        last_log = time.monotonic()
        while True:
            try:
                self._connect_probe()
                self.get_status()
            except (NRF24AdapterError, TimeoutError, AssertionError) as e:
                now = time.monotonic()
                if now >= deadline:
                    raise Exception(f"Failed to connect to {self.device_name}") from e
                if now - last_log >= 1.0:
                    logger.error(f"Connect failed, retrying for {deadline - now:.1f}s more")
                    last_log = now
                time.sleep(0.1)
                continue
            if self._connect_ready():
                break
            if time.monotonic() >= deadline:
                raise Exception(
                    f"Failed to connect to {self.device_name}: {self._connect_ready_error}"
                )
            logger.error(f"Connect: {self._connect_ready_error}, retrying")
            time.sleep(0.1)
        self._post_connect()
        logger.debug(f"{self.device_name} init status: {self._status}")
        logger.success(f"{self.device_name} Connected")

    def _connect_probe(self):
        """One round-trip that proves the device is answering. Raises on failure."""
        raise NotImplementedError

    def _connect_ready(self) -> bool:
        """Post-probe readiness check; connect() keeps retrying until True."""
        return True

    def _post_connect(self):
        """Optional best-effort seeding after the device is confirmed up."""
        pass
