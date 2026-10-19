"""OPC UA Connection Protocol and Secure Conversation (SF-070).

Clean-room implementation of OPC 10000-6 7.1 (UA TCP: HEL/ACK/ERR, message
chunks) and 6.7 (UA Secure Conversation) for security policies None and
Basic256Sha256 (OPC 10000-7 profile: RSA-PKCS1-v1.5-SHA256 asymmetric
signatures, RSA-OAEP-SHA1 asymmetric encryption, HMAC-SHA256, AES-256-CBC and
P_SHA256 key derivation from 32-byte nonces).

Policy None needs only the standard library. Basic256Sha256 needs the
`cryptography` package; when it is missing, a configuration that asks for it is
rejected rather than silently downgraded.
"""
import hashlib
import hmac
import os
from pathlib import Path
import socket
import struct
import threading
import time

from . import opcua_codec as ua

POLICY_NONE = "http://opcfoundation.org/UA/SecurityPolicy#None"
POLICY_BASIC256SHA256 = "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256"
POLICIES = {"None": POLICY_NONE, "Basic256Sha256": POLICY_BASIC256SHA256}
MODE_NONE, MODE_SIGN, MODE_SIGN_AND_ENCRYPT = 1, 2, 3
MODES = {"None": MODE_NONE, "Sign": MODE_SIGN, "SignAndEncrypt": MODE_SIGN_AND_ENCRYPT}
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
RSA_OAEP = "http://www.w3.org/2001/04/xmlenc#rsa-oaep"
TRANSPORT_PROFILE = "http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary"

BUFFER_SIZE = 65536
MAX_MESSAGE = 4 * 1024 * 1024
MAX_CHUNKS = 128
NONCE = 32


class ProtocolError(Exception):
    """The connection is unusable; status carries an OPC UA StatusCode when known."""

    def __init__(self, message, status=ua.STATUS["BadCommunicationError"]):
        super().__init__(message)
        self.status = status


class SecurityError(ProtocolError):
    def __init__(self, message, status=ua.STATUS["BadSecurityChecksFailed"]):
        super().__init__(message, status)


def crypto_available():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise SecurityError("security policy Basic256Sha256 requires the 'cryptography' package",
                            ua.STATUS["BadSecurityPolicyRejected"]) from None
    return x509, hashes, serialization, rsa_padding, rsa, Cipher, algorithms, modes


def load_certificate_der(path):
    data = Path(path).read_bytes()
    if data.lstrip().startswith(b"-----BEGIN"):
        x509 = _crypto()[0]
        from cryptography.hazmat.primitives.serialization import Encoding
        return x509.load_pem_x509_certificate(data).public_bytes(Encoding.DER)
    return data


def load_private_key(path):
    _, _, serialization, _, rsa, *_ = _crypto()
    data = Path(path).read_bytes()
    key = (serialization.load_pem_private_key if data.lstrip().startswith(b"-----BEGIN")
           else serialization.load_der_private_key)(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or not 2048 <= key.key_size <= 4096:
        raise SecurityError("Basic256Sha256 requires a 2048-4096 bit RSA private key")
    return key


def certificate_public_key(der):
    x509, _, _, _, rsa, *_ = _crypto()
    try:
        key = x509.load_der_x509_certificate(der).public_key()
    except ValueError:
        raise SecurityError("invalid certificate", ua.STATUS["BadCertificateInvalid"]) from None
    if not isinstance(key, rsa.RSAPublicKey) or not 2048 <= key.key_size <= 4096:
        raise SecurityError("certificate key is not 2048-4096 bit RSA", ua.STATUS["BadCertificateInvalid"])
    return key


def thumbprint(der):
    """ReceiverCertificateThumbprint: the X.509 SHA-1 fingerprint that OPC 10000-6 6.7.2.3
    prescribes. It only names the certificate; authentication comes from the pinned
    certificate bytes, the RSA signature and decryption with our private key."""
    x509, hashes, *_ = _crypto()
    try:
        return x509.load_der_x509_certificate(der).fingerprint(hashes.SHA1())
    except ValueError:
        raise SecurityError("invalid certificate", ua.STATUS["BadCertificateInvalid"]) from None


def p_sha256(secret, seed, length):
    """P_SHA256 from RFC 5246 5, as OPC 10000-6 6.7.5 uses for key derivation."""
    out, a = b"", seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return out[:length]


def derive_keys(secret, seed):
    material = p_sha256(secret, seed, 32 + 32 + 16)
    return {"signing": material[:32], "encrypting": material[32:64], "iv": material[64:]}


class Security:
    """Asymmetric half of a channel: our key/certificate and the peer certificate."""

    def __init__(self, policy_uri, mode, private_key=None, certificate=None, peer_certificate=None):
        if policy_uri == POLICY_NONE:
            if mode != MODE_NONE:
                raise SecurityError("policy None requires mode None", ua.STATUS["BadSecurityPolicyRejected"])
        elif policy_uri == POLICY_BASIC256SHA256:
            if mode not in (MODE_SIGN, MODE_SIGN_AND_ENCRYPT):
                raise SecurityError("Basic256Sha256 requires Sign or SignAndEncrypt", ua.STATUS["BadSecurityPolicyRejected"])
            _crypto()
            if private_key is None or certificate is None:
                raise SecurityError("Basic256Sha256 requires a certificate and private key")
        else:
            raise SecurityError(f"unsupported security policy {policy_uri}", ua.STATUS["BadSecurityPolicyRejected"])
        self.policy_uri, self.mode = policy_uri, mode
        self.private_key, self.certificate = private_key, certificate
        self.peer_certificate = peer_certificate

    @property
    def secure(self):
        return self.policy_uri != POLICY_NONE

    def peer_key(self):
        return certificate_public_key(self.peer_certificate)

    def sign(self, data):
        _, hashes, _, rsa_padding, *_ = _crypto()
        return self.private_key.sign(data, rsa_padding.PKCS1v15(), hashes.SHA256())

    @staticmethod
    def verify(certificate, signature, data):
        _, hashes, _, rsa_padding, *_ = _crypto()
        from cryptography.exceptions import InvalidSignature
        try:
            certificate_public_key(certificate).verify(signature, data, rsa_padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return False
        return True

    @staticmethod
    def _oaep():
        _, hashes, _, rsa_padding, *_ = _crypto()
        return rsa_padding.OAEP(mgf=rsa_padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None)

    @classmethod
    def encrypt_for(cls, certificate, data):
        key = certificate_public_key(certificate)
        size = key.key_size // 8
        block = size - 42
        if len(data) % block:
            raise ValueError("plaintext is not block aligned")
        return b"".join(key.encrypt(data[i:i + block], cls._oaep()) for i in range(0, len(data), block))

    def decrypt(self, data):
        size = self.private_key.key_size // 8
        if len(data) % size:
            raise SecurityError("ciphertext is not block aligned")
        try:
            return b"".join(self.private_key.decrypt(data[i:i + size], self._oaep()) for i in range(0, len(data), size))
        except ValueError:
            raise SecurityError("asymmetric decryption failed") from None

    def decrypt_secret(self, data, server_nonce):
        plain = self.decrypt(data)
        if len(plain) < 4:
            raise SecurityError("short secret")
        length = struct.unpack("<I", plain[:4])[0]
        if length != len(plain) - 4 or not plain.endswith(server_nonce) or length < len(server_nonce):
            raise SecurityError("secret nonce mismatch", ua.STATUS["BadIdentityTokenInvalid"])
        return plain[4:4 + length - len(server_nonce)]


def encrypt_secret(certificate, secret, server_nonce):
    """UserName password encryption (OPC 10000-4 7.41.2.3): length, secret, nonce, RSA-OAEP-SHA1."""
    plain = struct.pack("<I", len(secret) + len(server_nonce)) + secret + server_nonce
    key = certificate_public_key(certificate)
    block = key.key_size // 8 - 42
    return b"".join(key.encrypt(plain[i:i + block], Security._oaep()) for i in range(0, len(plain), block))


class SymmetricKeys:
    def __init__(self, token_id, local, remote):
        self.token_id, self.local, self.remote = token_id, local, remote


def _aes(key, iv, data, encrypt):
    *_, Cipher, algorithms, modes = _crypto()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


class Connection:
    """One UA TCP connection carrying one SecureChannel. Thread-safe sends."""

    def __init__(self, sock, security=None, max_message=MAX_MESSAGE):
        self.sock, self.security = sock, security
        self.send_lock, self.max_message = threading.Lock(), max_message
        self.channel_id, self.keys, self.accept = 0, None, {}
        self.send_sequence, self.receive_sequence = 1, None
        self.remote_buffer = BUFFER_SIZE
        self.closed = False

    # -- framing ----------------------------------------------------------
    def _recv_exact(self, n, deadline=None):
        out = bytearray()
        while len(out) < n:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("receive timeout", ua.STATUS["BadTimeout"])
                self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(n - len(out))
            except (socket.timeout, TimeoutError):
                raise ProtocolError("receive timeout", ua.STATUS["BadTimeout"]) from None
            except OSError as error:
                raise ProtocolError(f"receive failed: {error}") from None
            if not chunk:
                raise ProtocolError("connection closed by peer", ua.STATUS["BadSecureChannelClosed"])
            out.extend(chunk)
        return bytes(out)

    def read_chunk(self, deadline=None):
        header = self._recv_exact(8, deadline)
        kind, final, size = header[:3], header[3:4], struct.unpack("<I", header[4:])[0]
        if kind not in (b"HEL", b"ACK", b"ERR", b"MSG", b"OPN", b"CLO", b"RHE"):
            raise ProtocolError("unknown message type", ua.STATUS["BadTcpMessageTypeInvalid"])
        if final not in (b"F", b"C", b"A") or (kind in (b"HEL", b"ACK", b"ERR", b"OPN") and final != b"F"):
            raise ProtocolError("invalid chunk type", ua.STATUS["BadTcpMessageTypeInvalid"])
        if not 8 < size <= BUFFER_SIZE:
            raise ProtocolError("chunk size out of bounds", ua.STATUS["BadTcpMessageTooLarge"])
        return kind, final, header + self._recv_exact(size - 8, deadline)

    def send_raw(self, data):
        try:
            self.sock.sendall(data)
        except OSError as error:
            raise ProtocolError(f"send failed: {error}") from None

    def send_error(self, status, reason):
        w = ua.Writer()
        w.scalar("UInt32", status)
        ua.put_string(w, reason[:256])
        body = w.bytes()
        try:
            self.send_raw(b"ERRF" + struct.pack("<I", 8 + len(body)) + body)
        except ProtocolError:
            pass

    @staticmethod
    def parse_error(chunk):
        r = ua.Reader(chunk, 8)
        status = r.scalar("UInt32")
        return ProtocolError(f"server error {ua.status_name(status)}: {ua.get_string(r)}", status)

    def hello(self, endpoint_url, deadline):
        w = ua.Writer()
        for value in (0, BUFFER_SIZE, BUFFER_SIZE, self.max_message, MAX_CHUNKS):
            w.scalar("UInt32", value)
        ua.put_string(w, endpoint_url)
        body = w.bytes()
        self.send_raw(b"HELF" + struct.pack("<I", 8 + len(body)) + body)
        kind, _, chunk = self.read_chunk(deadline)
        if kind == b"ERR":
            raise self.parse_error(chunk)
        if kind != b"ACK" or len(chunk) != 28:
            raise ProtocolError("expected ACK")
        _version, receive_buffer, _send_buffer, _max_message, _max_chunks = struct.unpack("<5I", chunk[8:])
        if receive_buffer < 8192:
            raise ProtocolError("peer receive buffer below 8192")
        self.remote_buffer = min(receive_buffer, BUFFER_SIZE)

    def accept_hello(self, deadline):
        kind, _, chunk = self.read_chunk(deadline)
        if kind != b"HEL":
            raise ProtocolError("expected HEL", ua.STATUS["BadTcpMessageTypeInvalid"])
        r = ua.Reader(chunk, 8)
        _version, receive_buffer, _send_buffer, _max_message, _max_chunks = (r.scalar("UInt32") for _ in range(5))
        url = ua.get_string(r)
        if receive_buffer < 8192:
            raise ProtocolError("client receive buffer below 8192")
        self.remote_buffer = min(receive_buffer, BUFFER_SIZE)
        body = struct.pack("<5I", 0, BUFFER_SIZE, BUFFER_SIZE, self.max_message, MAX_CHUNKS)
        self.send_raw(b"ACKF" + struct.pack("<I", 8 + len(body)) + body)
        return url

    # -- sequence numbers ---------------------------------------------------
    def _next_sequence(self):
        value = self.send_sequence
        self.send_sequence = value + 1 if value < 4294966271 else 1
        return value

    def _check_sequence(self, value):
        previous = self.receive_sequence
        if previous is not None:
            wrapped = previous >= 4294966271 and value < 1024        # OPC 10000-6 6.7.2.4 rollover
            if value != previous + 1 and not wrapped:
                raise SecurityError("sequence number out of order", ua.STATUS["BadSequenceNumberInvalid"])
        self.receive_sequence = value

    # -- asymmetric (OPN) ---------------------------------------------------
    def send_open(self, request_id, body):
        sec = self.security
        header = ua.Writer()
        header.scalar("UInt32", self.channel_id)
        ua.put_string(header, sec.policy_uri)
        ua.put_string(header, sec.certificate if sec.secure else None)
        ua.put_string(header, thumbprint(sec.peer_certificate) if sec.secure else None)
        header = header.bytes()
        with self.send_lock:
            plain = struct.pack("<II", self._next_sequence(), request_id) + body
            if not sec.secure:
                self.send_raw(b"OPNF" + struct.pack("<I", 8 + len(header) + len(plain)) + header + plain)
                return
            peer_size = sec.peer_key().key_size // 8
            signature_size = sec.private_key.key_size // 8
            plain_block, extra = peer_size - 42, 1 if peer_size > 256 else 0
            rem = (len(plain) + signature_size + 1 + extra) % plain_block
            pad = plain_block - rem if rem else 0
            padding = bytes([pad & 0xFF]) * (pad + 1) + (bytes([pad >> 8]) if extra else b"")
            total = len(plain) + len(padding) + signature_size
            size = 8 + len(header) + total // plain_block * peer_size
            if size > self.remote_buffer:
                raise ProtocolError("OpenSecureChannel message exceeds the peer buffer")
            signed = b"OPNF" + struct.pack("<I", size) + header + plain + padding
            encrypted = sec.encrypt_for(sec.peer_certificate, plain + padding + sec.sign(signed))
            self.send_raw(signed[:8 + len(header)] + encrypted)

    def parse_open(self, chunk, trust=None):
        """Decode an OPN chunk. Returns (policy_uri, sender_certificate, request_id, body).

        On a server, `trust(policy_uri, certificate)` returns the Security to use
        (or raises); on a client the existing Security must match exactly.
        """
        r = ua.Reader(chunk, 8)
        channel_id = r.scalar("UInt32")
        policy_uri = ua.get_string(r)
        sender = ua.get_bytes(r)
        receiver = ua.get_bytes(r)
        header_end = r.offset
        if trust is not None:
            self.security = trust(policy_uri, sender)
        sec = self.security
        if policy_uri != sec.policy_uri:
            raise SecurityError("security policy changed", ua.STATUS["BadSecurityPolicyRejected"])
        if not sec.secure:
            if sender or receiver:
                raise SecurityError("certificates on a policy None channel")
            rest = chunk[header_end:]
        else:
            if sender != sec.peer_certificate:
                raise SecurityError("sender certificate is not the trusted peer", ua.STATUS["BadCertificateUntrusted"])
            if receiver != thumbprint(sec.certificate):
                raise SecurityError("message is not addressed to our certificate", ua.STATUS["BadCertificateInvalid"])
            decrypted = sec.decrypt(chunk[header_end:])
            signature_size = sec.peer_key().key_size // 8
            extra = 1 if sec.private_key.key_size // 8 > 256 else 0
            if len(decrypted) < 8 + signature_size + 1 + extra:
                raise SecurityError("short OpenSecureChannel message")
            signature = decrypted[-signature_size:]
            if not Security.verify(sender, signature, chunk[:header_end] + decrypted[:-signature_size]):
                raise SecurityError("asymmetric signature invalid")
            rest = self._unpad(decrypted[:-signature_size], extra)
        sequence, request_id = struct.unpack("<II", rest[:8])
        self._check_sequence(sequence)
        return channel_id, policy_uri, sender, request_id, rest[8:]

    @staticmethod
    def _unpad(data, extra=0):
        if not data:
            raise SecurityError("missing padding")
        if extra:
            pad = data[-1] << 8 | data[-2]
            low, strip = data[-2], pad + 2
        else:
            pad = data[-1]
            low, strip = pad, pad + 1
        if strip > len(data) or any(b != (low & 0xFF) for b in data[-strip:len(data) - extra]):
            raise SecurityError("invalid padding")
        return data[:-strip]

    # -- symmetric (MSG/CLO) -----------------------------------------------
    def install_keys(self, token_id, client_nonce, server_nonce, is_client, activate=True):
        """Derive keys for a token. A server renewing a token keeps sending with
        the old one until the client first uses the new one (OPC 10000-6 6.7.6)."""
        if self.security.secure:
            client_keys = derive_keys(server_nonce, client_nonce)
            server_keys = derive_keys(client_nonce, server_nonce)
            local, remote = (client_keys, server_keys) if is_client else (server_keys, client_keys)
        else:
            local = remote = None
        keys = SymmetricKeys(token_id, local, remote)
        self.accept[token_id] = keys
        if activate or self.keys is None:
            self.keys = keys

    def send_message(self, kind, request_id, body):
        keys, mode = self.keys, self.security.mode
        header = struct.pack("<II", self.channel_id, keys.token_id)
        if mode == MODE_SIGN_AND_ENCRYPT:
            room = (self.remote_buffer - 16) // 16 * 16 - 8 - 32 - 1
        elif mode == MODE_SIGN:
            room = self.remote_buffer - 16 - 8 - 32
        else:
            room = self.remote_buffer - 16 - 8
        pieces = [body[i:i + room] for i in range(0, len(body), room)] or [b""]
        if len(pieces) > MAX_CHUNKS:
            raise ProtocolError("message exceeds the chunk limit", ua.STATUS["BadTcpMessageTooLarge"])
        with self.send_lock:
            for index, piece in enumerate(pieces):
                final = b"F" if index == len(pieces) - 1 else b"C"
                plain = struct.pack("<II", self._next_sequence(), request_id) + piece
                if mode == MODE_NONE:
                    self.send_raw(kind + final + struct.pack("<I", 16 + len(plain)) + header + plain)
                    continue
                padding = b""
                if mode == MODE_SIGN_AND_ENCRYPT:
                    rem = (len(plain) + 32 + 1) % 16
                    pad = 16 - rem if rem else 0
                    padding = bytes([pad]) * (pad + 1)
                size = 16 + len(plain) + len(padding) + 32
                signed = kind + final + struct.pack("<I", size) + header + plain + padding
                signature = hmac.new(keys.local["signing"], signed, hashlib.sha256).digest()
                if mode == MODE_SIGN:
                    self.send_raw(signed + signature)
                else:
                    encrypted = _aes(keys.local["encrypting"], keys.local["iv"], plain + padding + signature, True)
                    self.send_raw(signed[:16] + encrypted)

    def parse_message(self, chunk):
        """Decode one MSG/CLO chunk -> (request_id, body piece)."""
        channel_id, token_id = struct.unpack("<II", chunk[8:16])
        if channel_id != self.channel_id:
            raise SecurityError("secure channel id mismatch", ua.STATUS["BadSecureChannelIdInvalid"])
        keys = self.accept.get(token_id)
        if keys is None:
            raise SecurityError("unknown security token", ua.STATUS["BadSecureChannelIdInvalid"])
        if keys.token_id >= self.keys.token_id:          # the peer uses the newest token: retire older ones
            self.keys, self.accept = keys, {keys.token_id: keys}
        mode = self.security.mode
        if mode == MODE_NONE:
            rest = chunk[16:]
        else:
            data = chunk[16:]
            if mode == MODE_SIGN_AND_ENCRYPT:
                if len(data) % 16:
                    raise SecurityError("ciphertext is not block aligned")
                data = _aes(keys.remote["encrypting"], keys.remote["iv"], data, False)
            if len(data) < 8 + 32:
                raise SecurityError("short message")
            expected = hmac.new(keys.remote["signing"], chunk[:16] + data[:-32], hashlib.sha256).digest()
            if not hmac.compare_digest(expected, data[-32:]):
                raise SecurityError("symmetric signature invalid")
            rest = data[:-32]
            if mode == MODE_SIGN_AND_ENCRYPT:
                rest = self._unpad(rest)
        if len(rest) < 8:
            raise ProtocolError("short sequence header", ua.STATUS["BadDecodingError"])
        sequence, request_id = struct.unpack("<II", rest[:8])
        self._check_sequence(sequence)
        return request_id, rest[8:]

    def read_message(self, deadline=None, trust=None):
        """Read one complete message: ('OPN'|'MSG'|'CLO'|'ERR', request_id, body)."""
        pieces, size, request = [], 0, None
        while True:
            kind, final, chunk = self.read_chunk(deadline)
            if kind == b"ERR":
                raise self.parse_error(chunk)
            if kind == b"OPN":
                channel_id, policy, sender, request_id, body = self.parse_open(chunk, trust)
                return "OPN", request_id, (channel_id, policy, sender, body)
            if kind not in (b"MSG", b"CLO"):
                raise ProtocolError("unexpected message type", ua.STATUS["BadTcpMessageTypeInvalid"])
            request_id, piece = self.parse_message(chunk)
            if request is not None and request_id != request:
                raise ProtocolError("interleaved chunks", ua.STATUS["BadDecodingError"])
            request = request_id
            if final == b"A":
                pieces, size, request = [], 0, None
                continue
            pieces.append(piece)
            size += len(piece)
            if size > self.max_message or len(pieces) > MAX_CHUNKS:
                raise ProtocolError("message too large", ua.STATUS["BadTcpMessageTooLarge"])
            if final == b"F":
                return kind.decode(), request_id, b"".join(pieces)

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def new_nonce():
    return os.urandom(NONCE)
