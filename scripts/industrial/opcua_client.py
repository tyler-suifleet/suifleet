"""OPC UA data-access client for the SF-070 collector (clean room).

Session establishment follows OPC 10000-4 5.4-5.6: GetEndpoints on a policy
None discovery channel, exact endpoint selection, OpenSecureChannel,
CreateSession and ActivateSession. Configured security is never downgraded:
the client refuses to continue unless the server offers exactly the configured
policy and mode, presents the pinned certificate, signs the session handshake
correctly and repeats the same endpoint list in CreateSession. A UserName
password is encrypted for the server certificate unless the configuration
explicitly allows plaintext.
"""
import itertools
import socket
import threading
import time
from urllib.parse import urlsplit

from . import opcua_codec as ua
from .opcua_secure import (MODE_NONE, POLICY_BASIC256SHA256, POLICY_NONE, RSA_OAEP, RSA_SHA256, TRANSPORT_PROFILE,
                           Connection, ProtocolError, Security, SecurityError, encrypt_secret, new_nonce)

APPLICATION_URI = "urn:suifleet:industrial-collector"
TOKEN_ANONYMOUS, TOKEN_USERNAME = 0, 1
ATTRIBUTE_VALUE = 13
TIMESTAMPS_BOTH = 2


class ServiceError(Exception):
    def __init__(self, service, status):
        super().__init__(f"{service} failed: {ua.status_name(status)}")
        self.service, self.status = service, status


class AuthenticationError(ServiceError):
    """Rejected identity or certificate; retrying cannot succeed without new configuration."""


def parse_endpoint(url):
    parts = urlsplit(url)
    if parts.scheme != "opc.tcp" or not parts.hostname:
        raise ValueError(f"unsupported endpoint URL {url!r}")
    return parts.hostname, parts.port or 4840


class Client:
    def __init__(self, endpoint_url, policy_uri=POLICY_NONE, mode=MODE_NONE, certificate=None, private_key=None,
                 server_certificate=None, identity=None, allow_plaintext_password=False,
                 connect_timeout=5.0, request_timeout=5.0, session_timeout_ms=60000, channel_lifetime_ms=3600000):
        self.endpoint_url = endpoint_url
        self.host, self.port = parse_endpoint(endpoint_url)
        self.policy_uri, self.mode = policy_uri, mode
        self.certificate, self.private_key, self.server_certificate = certificate, private_key, server_certificate
        self.identity = identity or {"type": "anonymous"}
        self.allow_plaintext_password = allow_plaintext_password
        self.connect_timeout, self.request_timeout = connect_timeout, request_timeout
        self.session_timeout_ms, self.channel_lifetime_ms = session_timeout_ms, channel_lifetime_ms
        self.conn, self.token = None, ua.NodeId()
        self.handles = itertools.count(1)
        self.request_ids = itertools.count(1)
        self.waiters, self.waiter_lock = {}, threading.Lock()
        self.broken, self.renew_at, self.renew_lock = None, None, threading.Lock()

    # -- transport --------------------------------------------------------
    def _open_socket(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as error:
            raise ProtocolError(f"connect failed: {error}") from None
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _header(self, timeout):
        return ua.make("RequestHeader", authentication_token=self.token, timestamp=ua.now_datetime(),
                       request_handle=next(self.handles), timeout_hint=int(timeout * 1000))

    def _open_channel(self, security, deadline):
        conn = Connection(self._open_socket(), security)
        try:
            conn.hello(self.endpoint_url, deadline)
            client_nonce = new_nonce() if security.secure else b""
            response = self._open_secure_channel(conn, 0, client_nonce, deadline)
            token = response["security_token"]
            conn.channel_id = token["channel_id"]
            conn.install_keys(token["token_id"], client_nonce, response["server_nonce"] or b"", True)
            lifetime = max(token["revised_lifetime"], 10000) / 1000
            return conn, time.monotonic() + 0.75 * lifetime
        except Exception:
            conn.close()
            raise

    def _open_secure_channel(self, conn, request_type, client_nonce, deadline):
        request = ua.make("OpenSecureChannelRequest", request_header=self._header(self.connect_timeout),
                          request_type=request_type, security_mode=conn.security.mode,
                          client_nonce=client_nonce, requested_lifetime=self.channel_lifetime_ms)
        request_id = next(self.request_ids)
        conn.send_open(request_id, ua.encode_message("OpenSecureChannelRequest", request))
        kind, got_id, payload = conn.read_message(deadline)
        if kind != "OPN" or got_id != request_id:
            raise ProtocolError("unexpected OpenSecureChannel answer")
        name, response = ua.decode_message(payload[3])
        self._check(name, response, "OpenSecureChannelResponse")
        nonce = response["server_nonce"] or b""
        if conn.security.secure and len(nonce) != 32:
            raise SecurityError("server nonce length", ua.STATUS["BadNonceInvalid"])
        return response

    def _check(self, name, response, expected):
        status = response["response_header"]["service_result"]
        if name == "ServiceFault" or status >> 31:
            raise ServiceError(expected.replace("Response", ""), status or ua.STATUS["BadUnexpectedError"])
        if name != expected:
            raise ProtocolError(f"expected {expected}, got {name}", ua.STATUS["BadDecodingError"])

    def _receive_loop(self, conn):
        try:
            while True:
                kind, request_id, body = conn.read_message()
                if kind == "OPN":
                    body = body[3]
                with self.waiter_lock:
                    waiter = self.waiters.pop(request_id, None)
                if waiter is not None:
                    waiter[1].append(body)
                    waiter[0].set()
        except Exception as error:                     # noqa: BLE001 - any failure ends the channel
            self.broken = error if isinstance(error, (ProtocolError, ua.DecodeError)) else ProtocolError(str(error))
            with self.waiter_lock:
                waiters, self.waiters = list(self.waiters.values()), {}
            for event, _ in waiters:
                event.set()

    def request(self, name, fields, timeout=None):
        """Send one service request and wait for its response."""
        timeout = timeout or self.request_timeout
        if self.broken is not None or self.conn is None:
            raise self.broken or ProtocolError("not connected")
        if self.renew_at is not None and time.monotonic() >= self.renew_at:
            self._renew()
        fields = dict(fields)
        fields["request_header"] = self._header(timeout)
        request_id = next(self.request_ids)
        event, box = threading.Event(), []
        with self.waiter_lock:
            self.waiters[request_id] = (event, box)
        self.conn.send_message(b"MSG", request_id, ua.encode_message(name, ua.make(name, **fields)))
        if not event.wait(timeout + 1):
            with self.waiter_lock:
                self.waiters.pop(request_id, None)
            raise ProtocolError(f"{name} timed out", ua.STATUS["BadTimeout"])
        if not box:
            raise self.broken or ProtocolError("channel closed")
        try:
            response_name, response = ua.decode_message(box[0])
            self._check(response_name, response, name.replace("Request", "Response"))
        except (ua.DecodeError, ProtocolError) as error:
            self._fail(error)                  # a peer sending undecodable data is not trusted further
            raise
        return response

    def _fail(self, error):
        if self.broken is None:
            self.broken = error if isinstance(error, ProtocolError) else ProtocolError(f"decode error: {error}",
                                                                                         ua.STATUS["BadDecodingError"])
        conn = self.conn
        if conn is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _renew(self):
        """Renew the security token before it expires (OPC 10000-6 6.7.6)."""
        with self.renew_lock:
            if time.monotonic() < self.renew_at:
                return
            conn = self.conn
            client_nonce = new_nonce() if conn.security.secure else b""
            request = ua.make("OpenSecureChannelRequest", request_header=self._header(self.request_timeout),
                              request_type=1, security_mode=conn.security.mode, client_nonce=client_nonce,
                              requested_lifetime=self.channel_lifetime_ms)
            request_id = next(self.request_ids)
            event, box = threading.Event(), []
            with self.waiter_lock:
                self.waiters[request_id] = (event, box)
            conn.send_open(request_id, ua.encode_message("OpenSecureChannelRequest", request))
            if not event.wait(self.request_timeout + 1) or not box:
                raise self.broken or ProtocolError("token renewal timed out", ua.STATUS["BadTimeout"])
            name, response = ua.decode_message(box[0])
            self._check(name, response, "OpenSecureChannelResponse")
            token = response["security_token"]
            if token["channel_id"] != conn.channel_id:
                raise SecurityError("renewal changed the channel id")
            conn.install_keys(token["token_id"], client_nonce, response["server_nonce"] or b"", True)
            self.renew_at = time.monotonic() + 0.75 * max(token["revised_lifetime"], 10000) / 1000

    # -- session ------------------------------------------------------------
    def discover(self):
        """GetEndpoints over a policy None discovery channel."""
        deadline = time.monotonic() + self.connect_timeout
        conn, _ = self._open_channel(Security(POLICY_NONE, MODE_NONE), deadline)
        try:
            request_id = next(self.request_ids)
            body = ua.make("GetEndpointsRequest", request_header=self._header(self.connect_timeout),
                           endpoint_url=self.endpoint_url)
            conn.send_message(b"MSG", request_id, ua.encode_message("GetEndpointsRequest", body))
            kind, got_id, payload = conn.read_message(deadline)
            if kind != "MSG" or got_id != request_id:
                raise ProtocolError("unexpected GetEndpoints answer")
            name, response = ua.decode_message(payload)
            self._check(name, response, "GetEndpointsResponse")
            try:
                conn.send_message(b"CLO", next(self.request_ids), ua.encode_message(
                    "CloseSecureChannelRequest", ua.make("CloseSecureChannelRequest")))
            except ProtocolError:
                pass
            return response["endpoints"]
        finally:
            conn.close()

    def select_endpoint(self, endpoints):
        matches = [e for e in endpoints if e["security_policy_uri"] == self.policy_uri and e["security_mode"] == self.mode
                   and e["transport_profile_uri"] in (TRANSPORT_PROFILE, None, "")]
        if not matches:
            raise SecurityError("server does not offer the configured security policy and mode",
                                ua.STATUS["BadSecurityPolicyRejected"])
        endpoint = matches[0]
        if self.policy_uri != POLICY_NONE and endpoint["server_certificate"] != self.server_certificate:
            raise SecurityError("server certificate does not match the pinned certificate",
                                ua.STATUS["BadCertificateUntrusted"])
        wanted = TOKEN_USERNAME if self.identity["type"] == "username" else TOKEN_ANONYMOUS
        policies = [p for p in endpoint["user_identity_tokens"] if p["token_type"] == wanted]
        if not policies:
            raise AuthenticationError("ActivateSession", ua.STATUS["BadIdentityTokenRejected"])
        return endpoint, policies[0]

    def connect(self):
        endpoints = self.discover()
        endpoint, token_policy = self.select_endpoint(endpoints)
        deadline = time.monotonic() + self.connect_timeout
        security = Security(self.policy_uri, self.mode, self.private_key, self.certificate, self.server_certificate)
        self.conn, self.renew_at = self._open_channel(security, deadline)
        self.broken = None
        threading.Thread(target=self._receive_loop, args=(self.conn,), daemon=True).start()
        try:
            self._create_and_activate(endpoints, endpoint, token_policy)
        except Exception:
            self.close()
            raise

    def _create_and_activate(self, endpoints, endpoint, token_policy):
        secure = self.policy_uri != POLICY_NONE
        client_nonce = new_nonce()
        created = self.request("CreateSessionRequest", dict(
            client_description=ua.make("ApplicationDescription", application_uri=APPLICATION_URI,
                                       product_uri="urn:suifleet", application_name="SuiFleet industrial collector",
                                       application_type=1),
            endpoint_url=self.endpoint_url, session_name="suifleet-collector", client_nonce=client_nonce,
            client_certificate=self.certificate if secure else None,
            requested_session_timeout=float(self.session_timeout_ms), max_response_message_size=0))
        server_nonce = created["server_nonce"] or b""
        if secure:
            if created["server_certificate"] != self.server_certificate:
                raise SecurityError("CreateSession certificate does not match the pinned certificate",
                                    ua.STATUS["BadCertificateUntrusted"])
            signature = created["server_signature"]
            if signature["algorithm"] != RSA_SHA256 or not Security.verify(
                    self.server_certificate, signature["signature"] or b"", self.certificate + client_nonce):
                raise SecurityError("server session signature invalid", ua.STATUS["BadApplicationSignatureInvalid"])
            if len(server_nonce) < 32:
                raise SecurityError("server session nonce", ua.STATUS["BadNonceInvalid"])

        def offered(items):
            return sorted((e["security_policy_uri"], e["security_mode"]) for e in items)
        if created["server_endpoints"] and offered(created["server_endpoints"]) != offered(
                [e for e in endpoints if e["endpoint_url"] == endpoint["endpoint_url"]] or endpoints):
            raise SecurityError("CreateSession endpoints differ from discovery (possible downgrade)")
        self.token = created["authentication_token"]
        client_signature = ua.make("SignatureData")
        if secure:
            client_signature = ua.make("SignatureData", algorithm=RSA_SHA256,
                                       signature=self.conn.security.sign(self.server_certificate + server_nonce))
        try:
            self.request("ActivateSessionRequest", dict(
                client_signature=client_signature, locale_ids=["en"],
                user_identity_token=self._identity_token(endpoint, token_policy, server_nonce)))
        except ServiceError as error:
            if error.status in (ua.STATUS["BadUserAccessDenied"], ua.STATUS["BadIdentityTokenRejected"],
                                ua.STATUS["BadIdentityTokenInvalid"], ua.STATUS["BadApplicationSignatureInvalid"],
                                ua.STATUS["BadSecurityChecksFailed"], ua.STATUS["BadCertificateUntrusted"]):
                raise AuthenticationError("ActivateSession", error.status) from None
            raise

    def _identity_token(self, endpoint, token_policy, server_nonce):
        if self.identity["type"] == "anonymous":
            return ua.ExtensionObject(ua.NodeId(0, ua.ENCODING_IDS["AnonymousIdentityToken"]),
                                      {"policy_id": token_policy["policy_id"]})
        policy = token_policy["security_policy_uri"] or endpoint["security_policy_uri"]
        password = self.identity["password"]
        if policy == POLICY_NONE:
            if not self.allow_plaintext_password:
                raise SecurityError("refusing to send a password without encryption; the server's token policy is None",
                                    ua.STATUS["BadSecurityPolicyRejected"])
            secret, algorithm = password, None
        elif policy == POLICY_BASIC256SHA256:
            if self.server_certificate is None or endpoint["server_certificate"] != self.server_certificate:
                raise SecurityError("password encryption needs the pinned server certificate",
                                    ua.STATUS["BadCertificateUntrusted"])
            if len(server_nonce) < 32:
                raise SecurityError("server session nonce", ua.STATUS["BadNonceInvalid"])
            secret, algorithm = encrypt_secret(self.server_certificate, password, server_nonce), RSA_OAEP
        else:
            raise SecurityError(f"unsupported user token security policy {policy}",
                                ua.STATUS["BadSecurityPolicyRejected"])
        return ua.ExtensionObject(ua.NodeId(0, ua.ENCODING_IDS["UserNameIdentityToken"]), {
            "policy_id": token_policy["policy_id"], "user_name": self.identity["username"],
            "password": secret, "encryption_algorithm": algorithm})

    # -- services -----------------------------------------------------------
    def read(self, node_ids):
        response = self.request("ReadRequest", dict(
            max_age=0.0, timestamps_to_return=TIMESTAMPS_BOTH,
            nodes_to_read=[ua.make("ReadValueId", node_id=n, attribute_id=ATTRIBUTE_VALUE) for n in node_ids]))
        if len(response["results"]) != len(node_ids):
            raise ProtocolError("Read result count", ua.STATUS["BadDecodingError"])
        return response["results"]

    def create_subscription(self, publishing_interval_ms, lifetime_count, keepalive_count, max_notifications=0):
        response = self.request("CreateSubscriptionRequest", dict(
            requested_publishing_interval=float(publishing_interval_ms), requested_lifetime_count=lifetime_count,
            requested_max_keep_alive_count=keepalive_count, max_notifications_per_publish=max_notifications,
            publishing_enabled=True, priority=0))
        return response

    def create_monitored_items(self, subscription_id, items):
        """items: [(client_handle, node_id, sampling_interval_ms, queue_size)]."""
        response = self.request("CreateMonitoredItemsRequest", dict(
            subscription_id=subscription_id, timestamps_to_return=TIMESTAMPS_BOTH,
            items_to_create=[ua.make("MonitoredItemCreateRequest",
                                     item_to_monitor=ua.make("ReadValueId", node_id=node, attribute_id=ATTRIBUTE_VALUE),
                                     monitoring_mode=2,
                                     requested_parameters=ua.make("MonitoringParameters", client_handle=handle,
                                                                  sampling_interval=float(sampling), queue_size=queue,
                                                                  discard_oldest=True))
                             for handle, node, sampling, queue in items]))
        if len(response["results"]) != len(items):
            raise ProtocolError("CreateMonitoredItems result count", ua.STATUS["BadDecodingError"])
        return response["results"]

    def publish(self, acknowledgements, timeout):
        return self.request("PublishRequest", dict(subscription_acknowledgements=[
            ua.make("SubscriptionAcknowledgement", subscription_id=s, sequence_number=n) for s, n in acknowledgements]),
            timeout=timeout)

    def close(self):
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            if self.token != ua.NodeId() and self.broken is None:
                self.conn = conn
                self.request("CloseSessionRequest", dict(delete_subscriptions=True), timeout=1.0)
                self.conn = None
                conn.send_message(b"CLO", next(self.request_ids), ua.encode_message(
                    "CloseSecureChannelRequest", ua.make("CloseSecureChannelRequest")))
        except (ProtocolError, ServiceError, ua.DecodeError):
            pass
        finally:
            self.conn, self.token = None, ua.NodeId()
            conn.close()
