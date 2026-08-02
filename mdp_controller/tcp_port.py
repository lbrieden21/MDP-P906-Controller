import socket
import threading
import time
from typing import Optional

from loguru import logger


class TcpAdapterPort:
    """Duck-types the pyserial subset touched below `NRF24Adapter`  --
    ``write()``, ``read(n)``, ``in_waiting``, ``close()``, a settable
    ``baudrate`` -- so ``open_adapter_port()`` can hand back a socket-backed
    object in place of a ``serial.Serial`` and nothing above this class has
    to change. See plans/nrf_adapter_esp32_wifi_link_plan.md, "Host side".

    A background thread owns the socket: it appends inbound bytes into a
    bytearray under a lock, and reconnects with backoff on a drop. The
    adapter keeps its radio configuration across a drop -- it lives on the
    device, not the host -- so no re-init is needed once the reconnect
    lands; `NRF24Adapter`'s own ECHO-driven `_connect_event` notices the gap
    and the recovery on its own.
    """

    def __init__(self, host: str, port: int, connect_timeout: float = 2.0):
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        # Accepted, ignored: host_link_wifi.c has no line rate to set, same
        # as every USB CDC target (see host_link_usb_jtag.c's baudrate stub).
        self.baudrate = 0

        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = True

        # Synchronous first connect: MDPBus.__init__ blocks on the GUI
        # thread, so a wrong host/port must fail fast rather than surface
        # only once the worker thread's first retry gives up.
        self._sock = self._connect()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _connect(self) -> socket.socket:
        sock = socket.create_connection(
            (self._host, self._port), timeout=self._connect_timeout
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.5)
        return sock

    def _worker(self):
        sock = self._sock
        backoff = 0.5
        while self._running:
            try:
                if sock is None:
                    sock = self._connect()
                    with self._lock:
                        self._sock = sock
                    logger.success("TcpAdapterPort reconnected")
                    backoff = 0.5
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("socket closed by peer")
                with self._lock:
                    self._buf += data
            except socket.timeout:
                continue
            except OSError as e:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                sock = None
                with self._lock:
                    self._sock = None
                if not self._running:
                    # close() closing the socket out from under a blocked
                    # recv() lands here too -- not a real connection loss.
                    break
                logger.warning(f"TcpAdapterPort connection lost ({e}), reconnecting")
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            data = bytes(self._buf[:n])
            del self._buf[:n]
        return data

    def write(self, data: bytes) -> None:
        with self._lock:
            sock = self._sock
        if sock is None:
            # Dropped mid-reconnect -- the caller's own retry/timeout loop
            # (MDPBus.transfer / NRF24Adapter._action) covers this exactly
            # like a wired timeout would.
            return
        try:
            sock.sendall(data)
        except OSError:
            pass  # the worker thread notices on its next recv() and reconnects

    def close(self):
        self._running = False
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._thread.join(timeout=2)


def open_tcp_adapter_port(url: str, connect_timeout: Optional[float] = 2.0) -> TcpAdapterPort:
    """Parse a `tcp://host:port` URL and open it."""
    host, _, port_str = url[len("tcp://"):].partition(":")
    return TcpAdapterPort(host, int(port_str), connect_timeout=connect_timeout or 2.0)
