"""Clean-room Modbus client and deterministic simulator (SF-070). Stdlib only.

Implemented from the MODBUS Application Protocol Specification V1.1b3
(2012-04-26), the MODBUS Messaging on TCP/IP Implementation Guide V1.0b
(2006-10-24) and MODBUS over Serial Line V1.02 (2006-12-20). Read function
codes 01-04 only: the collector never writes to a device. Transports: plain
TCP (MBAP), TLS with mutual certificate authentication (the MODBUS/TCP Security
framing: MBAP inside TLS, conventionally port 802) and RTU (CRC-16 framing on a
serial device or pseudo-terminal).

Every response is checked before use. A wrong transaction identifier,
protocol identifier, length, unit, function code, byte count or CRC raises
MalformedResponse; the caller closes the connection, because the byte stream can
no longer be trusted to be in step.
"""
import os
import select
import socket
import ssl
import struct
import termios
import threading
import time

READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS = 1, 2, 3, 4
TABLES = {"coil": READ_COILS, "discrete_input": READ_DISCRETE_INPUTS,
          "holding_register": READ_HOLDING_REGISTERS, "input_register": READ_INPUT_REGISTERS}
MAX_BITS, MAX_REGISTERS = 2000, 125            # V1.1b3 6.1-6.4 quantity limits
MAX_ADU = 260                                  # TCP ADU: 7-byte MBAP + 253-byte PDU
REGISTER_WIDTH = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2,
                  "int64": 4, "uint64": 4, "float64": 4}
EXCEPTIONS = {1: "illegal-function", 2: "illegal-data-address", 3: "illegal-data-value",
              4: "server-device-failure", 5: "acknowledge", 6: "server-device-busy",
              8: "memory-parity-error", 10: "gateway-path-unavailable", 11: "gateway-target-failed"}


class ModbusError(Exception):
    """Transport failure: the connection must be closed and reopened."""


class MalformedResponse(ModbusError):
    pass


class ResponseTimeout(ModbusError):
    pass


class ExceptionResponse(Exception):
    """A well-formed exception response; the connection stays usable."""

    def __init__(self, function, code):
        super().__init__(f"exception {code} ({EXCEPTIONS.get(code, 'unknown')}) for function {function}")
        self.function, self.code = function, code


def crc16(data):
    """CRC-16/MODBUS (polynomial 0xA001 reflected, initial 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_request(function, address, quantity):
    limit = MAX_BITS if function in (READ_COILS, READ_DISCRETE_INPUTS) else MAX_REGISTERS
    if function not in (1, 2, 3, 4) or not 0 <= address <= 0xFFFF or not 1 <= quantity <= limit or address + quantity > 0x10000:
        raise ValueError("invalid read request")
    return struct.pack(">BHH", function, address, quantity)


def expected_bytes(function, quantity):
    return (quantity + 7) // 8 if function in (READ_COILS, READ_DISCRETE_INPUTS) else 2 * quantity


def parse_read_response(function, quantity, pdu):
    """Return a list of bools (bit tables) or ints (register tables)."""
    if not pdu:
        raise MalformedResponse("empty PDU")
    if pdu[0] == function | 0x80:
        if len(pdu) != 2:
            raise MalformedResponse("exception PDU length")
        raise ExceptionResponse(function, pdu[1])
    if pdu[0] != function:
        raise MalformedResponse(f"function {pdu[0]} answered request {function}")
    count = expected_bytes(function, quantity)
    if len(pdu) < 2 or pdu[1] != count or len(pdu) != 2 + count:
        raise MalformedResponse("byte count")
    data = pdu[2:]
    if function in (READ_COILS, READ_DISCRETE_INPUTS):
        if quantity % 8 and data[-1] >> (quantity % 8):
            raise MalformedResponse("padding bits set")
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(quantity)]
    return list(struct.unpack(f">{quantity}H", data))


def decode_registers(kind, registers, word_order="big"):
    """Big-endian bytes within a register (V1.1b3 4.2); word order is per device."""
    if len(registers) != REGISTER_WIDTH[kind]:
        raise ValueError("register count")
    words = registers if word_order == "big" else list(reversed(registers))
    raw = b"".join(struct.pack(">H", w) for w in words)
    return struct.unpack({"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f",
                          "int64": ">q", "uint64": ">Q", "float64": ">d"}[kind], raw)[0]


def encode_registers(kind, value, word_order="big"):
    raw = struct.pack({"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f",
                       "int64": ">q", "uint64": ">Q", "float64": ">d"}[kind], value)
    words = [struct.unpack(">H", raw[i:i + 2])[0] for i in range(0, len(raw), 2)]
    return words if word_order == "big" else list(reversed(words))


def _recv_exact(sock, n, deadline):
    out = bytearray()
    while len(out) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResponseTimeout("response timeout")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(out))
        except (socket.timeout, TimeoutError):
            raise ResponseTimeout("response timeout") from None
        except ssl.SSLError as error:          # TLS 1.3 reports a refused client certificate after the handshake
            raise ModbusError(f"TLS error: {error}") from None
        except OSError as error:
            raise ModbusError(f"receive failed: {error}") from None
        if not chunk:
            raise ModbusError("connection closed by peer")
        out.extend(chunk)
    return bytes(out)


class TcpClient:
    """MBAP over TCP, or over TLS when an ssl.SSLContext is supplied."""

    def __init__(self, host, port, unit, connect_timeout, request_timeout, tls=None, server_name=None):
        self.host, self.port, self.unit = host, port, unit
        self.connect_timeout, self.request_timeout = connect_timeout, request_timeout
        self.tls, self.server_name, self.sock, self.transaction = tls, server_name, None, 0

    def connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as error:
            raise ModbusError(f"connect failed: {error}") from None
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.tls is not None:
                sock.settimeout(self.connect_timeout)
                sock = self.tls.wrap_socket(sock, server_hostname=self.server_name or self.host)
        except (OSError, ssl.SSLError) as error:
            sock.close()
            raise ModbusError(f"TLS handshake failed: {error}") from None
        self.sock = sock

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def read(self, function, address, quantity):
        if self.sock is None:
            raise ModbusError("not connected")
        pdu = read_request(function, address, quantity)
        self.transaction = (self.transaction + 1) & 0xFFFF
        frame = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, self.unit) + pdu
        deadline = time.monotonic() + self.request_timeout
        try:
            self.sock.settimeout(self.request_timeout)
            self.sock.sendall(frame)
        except (OSError, ssl.SSLError) as error:
            raise ModbusError(f"send failed: {error}") from None
        header = _recv_exact(self.sock, 7, deadline)
        transaction, protocol, length, unit = struct.unpack(">HHHB", header)
        if transaction != self.transaction:
            raise MalformedResponse(f"transaction {transaction}, expected {self.transaction}")
        if protocol != 0:
            raise MalformedResponse("protocol identifier")
        if not 2 <= length <= MAX_ADU - 6:
            raise MalformedResponse("MBAP length")
        if unit != self.unit:
            raise MalformedResponse("unit identifier")
        return parse_read_response(function, quantity, _recv_exact(self.sock, length - 1, deadline))


SERIAL_BAUD = {1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600,
               19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}


def configure_serial(fd, baud, data_bits=8, parity="N", stop_bits=1):
    """Raw 8N1-style line settings with termios; no flow control."""
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag = 0, 0, termios.CLOCAL | termios.CREAD, 0
    cflag |= {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}[data_bits]
    if parity in ("E", "O"):
        cflag |= termios.PARENB | (termios.PARODD if parity == "O" else 0)
    if stop_bits == 2:
        cflag |= termios.CSTOPB
    attrs[0:4] = [iflag, oflag, cflag, lflag]
    attrs[4] = attrs[5] = SERIAL_BAUD[baud]
    attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


class RtuClient:
    """RTU framing on a serial device. Responses are delimited by expected length."""

    def __init__(self, device, unit, request_timeout, baud=9600, data_bits=8, parity="N", stop_bits=1):
        self.device, self.unit, self.request_timeout = device, unit, request_timeout
        self.line = (baud, data_bits, parity, stop_bits)
        self.fd = None
        # 3.5 character times of silence between frames (V1.02 2.5.1.1); fixed 1.75 ms above 19200 baud.
        bits = 1 + data_bits + (parity != "N") + stop_bits
        self.silence = 0.00175 if baud > 19200 else 3.5 * bits / baud

    def connect(self):
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as error:
            raise ModbusError(f"open failed: {error}") from None
        try:
            configure_serial(fd, *self.line)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except (OSError, termios.error) as error:
            os.close(fd)
            raise ModbusError(f"serial setup failed: {error}") from None
        self.fd = fd

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def _read(self, n, deadline):
        out = bytearray()
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseTimeout("response timeout")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, n - len(out))
            except BlockingIOError:
                continue
            except OSError as error:
                raise ModbusError(f"serial read failed: {error}") from None
            if not chunk:
                raise ModbusError("serial device closed")
            out.extend(chunk)
        return bytes(out)

    def read(self, function, address, quantity):
        if self.fd is None:
            raise ModbusError("not connected")
        if not 1 <= self.unit <= 247:
            raise ValueError("RTU requests need a unicast unit 1-247")
        body = bytes([self.unit]) + read_request(function, address, quantity)
        time.sleep(self.silence)
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
            os.write(self.fd, body + struct.pack("<H", crc16(body)))
        except OSError as error:
            raise ModbusError(f"serial write failed: {error}") from None
        deadline = time.monotonic() + self.request_timeout
        head = self._read(2, deadline)
        if head[0] != self.unit:
            raise MalformedResponse("unit identifier")
        if head[1] == function | 0x80:
            rest = self._read(3, deadline)
        elif head[1] == function:
            count = self._read(1, deadline)
            rest = count + self._read(count[0] + 2, deadline)
        else:
            raise MalformedResponse(f"function {head[1]} answered request {function}")
        frame = head + rest
        if struct.unpack("<H", frame[-2:])[0] != crc16(frame[:-2]):
            raise MalformedResponse("CRC mismatch")
        return parse_read_response(function, quantity, frame[1:-2])


# --- Deterministic simulator -------------------------------------------------

class DataModel:
    """Four independent 65536-entry tables, values set explicitly or by tick()."""

    def __init__(self, unit=1):
        self.unit, self.lock = unit, threading.Lock()
        self.bits = {READ_COILS: {}, READ_DISCRETE_INPUTS: {}}
        self.registers = {READ_HOLDING_REGISTERS: {}, READ_INPUT_REGISTERS: {}}
        self.exceptions = {}        # (function, address) -> exception code
        self.ticks = 0

    def set_registers(self, table, address, words):
        with self.lock:
            for i, w in enumerate(words):
                self.registers[TABLES[table]][address + i] = w & 0xFFFF

    def set_value(self, table, address, kind, value, word_order="big"):
        self.set_registers(table, address, encode_registers(kind, value, word_order))

    def set_bit(self, table, address, value):
        with self.lock:
            self.bits[TABLES[table]][address] = bool(value)

    def tick(self):
        """Deterministic generator: input register 0 counts ticks, coil 0 toggles."""
        with self.lock:
            self.ticks += 1
            self.registers[READ_INPUT_REGISTERS][0] = self.ticks & 0xFFFF
            self.bits[READ_COILS][0] = bool(self.ticks & 1)

    def answer(self, pdu):
        """PDU -> response PDU, following V1.1b3 6.1-6.4 and 7."""
        if len(pdu) < 1:
            return None
        function = pdu[0]
        if function not in (1, 2, 3, 4):
            return bytes([function | 0x80, 1])
        if len(pdu) != 5:
            return bytes([function | 0x80, 3])
        address, quantity = struct.unpack(">HH", pdu[1:])
        limit = MAX_BITS if function in (1, 2) else MAX_REGISTERS
        if not 1 <= quantity <= limit:
            return bytes([function | 0x80, 3])
        if address + quantity > 0x10000:
            return bytes([function | 0x80, 2])
        with self.lock:
            for a in range(address, address + quantity):
                if (function, a) in self.exceptions:
                    return bytes([function | 0x80, self.exceptions[(function, a)]])
            if function in (1, 2):
                values = [self.bits[function].get(a, False) for a in range(address, address + quantity)]
                data = bytearray((quantity + 7) // 8)
                for i, v in enumerate(values):
                    data[i // 8] |= v << (i % 8)
                return bytes([function, len(data)]) + bytes(data)
            words = [self.registers[function].get(a, 0) for a in range(address, address + quantity)]
            return bytes([function, 2 * quantity]) + struct.pack(f">{quantity}H", *words)


class Simulator:
    """Threaded Modbus TCP/TLS server with fault injection.

    fault: None, "stall" (no answer), "wrong-transaction", "wrong-unit",
    "bad-length", "bad-byte-count", "garbage" or "close"; applied to the next
    request only. Requests for another unit are not answered, as a gateway
    without that target would not.
    """

    def __init__(self, model=None, host="127.0.0.1", port=0, tls=None):
        self.model = model or DataModel()
        self.tls, self.fault, self.requests = tls, None, 0
        self.connections, self.lock, self.stopped = set(), threading.Lock(), False
        self.server = socket.create_server((host, port))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopped:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        try:
            if self.tls is not None:
                sock.settimeout(5)
                sock = self.tls.wrap_socket(sock, server_side=True)
                sock.settimeout(None)
        except (OSError, ssl.SSLError):
            sock.close()
            return
        with self.lock:
            self.connections.add(sock)
        try:
            while True:
                header = self._exact(sock, 7)
                if header is None:
                    return
                transaction, protocol, length, unit = struct.unpack(">HHHB", header)
                if protocol != 0 or not 2 <= length <= 254:
                    return
                pdu = self._exact(sock, length - 1)
                if pdu is None:
                    return
                with self.lock:
                    self.requests += 1
                    fault, self.fault = self.fault, None
                if unit != self.model.unit and unit != 0:
                    continue
                reply = self.model.answer(pdu)
                if fault == "stall":
                    continue
                if fault == "close":
                    return
                if fault == "garbage":
                    sock.sendall(b"\xde\xad\xbe\xef\x00\x00\x00")
                    continue
                if fault == "bad-byte-count" and len(reply) > 2:
                    reply = reply[:1] + bytes([reply[1] + 2]) + reply[2:]
                if fault == "wrong-transaction":
                    transaction ^= 0x5A5A
                if fault == "wrong-unit":
                    unit ^= 0x01
                size = len(reply) + 1
                if fault == "bad-length":
                    size = 300
                sock.sendall(struct.pack(">HHHB", transaction, 0, size, unit) + reply)
        except (OSError, ssl.SSLError):
            return
        finally:
            with self.lock:
                self.connections.discard(sock)
            sock.close()

    @staticmethod
    def _exact(sock, n):
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                return None
            out.extend(chunk)
        return bytes(out)

    def drop_connections(self):
        with self.lock:
            connections = list(self.connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self.stopped = True
        try:
            self.server.shutdown(socket.SHUT_RDWR)     # wakes the blocked accept() so the port really closes
        except OSError:
            pass
        self.server.close()
        self.drop_connections()


class RtuSimulator:
    """RTU server on the master side of a pseudo-terminal pair."""

    def __init__(self, model=None):
        self.model = model or DataModel()
        self.fault, self.requests, self.stopped = None, 0, False
        self.master, slave = os.openpty()
        self.device = os.ttyname(slave)
        configure_serial(slave, 115200)
        self._slave = slave            # keep the pty open while clients come and go
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        buffer = bytearray()
        while not self.stopped:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    buffer.extend(os.read(self.master, 512))
                except OSError:
                    return
                continue
            if not buffer:
                continue
            frame, buffer = bytes(buffer), bytearray()     # a silent interval ends the frame
            if len(frame) < 4 or struct.unpack("<H", frame[-2:])[0] != crc16(frame[:-2]):
                continue                                    # V1.02: a bad frame is discarded silently
            if frame[0] != self.model.unit:
                continue
            self.requests += 1
            fault, self.fault = self.fault, None
            if fault == "stall":
                continue
            body = bytes([frame[0]]) + self.model.answer(frame[1:-2])
            crc = crc16(body)
            if fault == "bad-crc":
                crc ^= 0xFFFF
            os.write(self.master, body + struct.pack("<H", crc))

    def close(self):
        self.stopped = True
        self.thread.join(timeout=2)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
