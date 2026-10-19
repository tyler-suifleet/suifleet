"""Deterministic OPC UA server simulator for SF-070 tests (clean room).

A small address space of Variable nodes whose values change only through
set_value() or tick(). It implements the services the collector uses:
GetEndpoints, OpenSecureChannel (issue/renew), CreateSession, ActivateSession,
CloseSession, Read, CreateSubscription, CreateMonitoredItems, Publish and
DeleteSubscriptions, with OPC 10000-4 5.13 subscription keep-alive/lifetime
counters and bounded monitored-item queues (discard oldest, overflow bit).

Fault controls: drop_connections(), expire_subscriptions(), restart() (forgets
sessions), corrupt_next ("chunk" | "body") and per-node status codes.
It is a test fixture, not a general OPC UA server.
"""
import hmac
import itertools
import socket
import struct
import threading
import time

from . import opcua_codec as ua
from .opcua_secure import (MODE_NONE, MODES, POLICIES, POLICY_NONE, RSA_OAEP, RSA_SHA256, TRANSPORT_PROFILE,
                           Connection, ProtocolError, Security, SecurityError, new_nonce)

SERVER_URI = "urn:suifleet:opcua-simulator"


class Node:
    __slots__ = ("vtype", "value", "status", "source_timestamp")

    def __init__(self, vtype, value, status=ua.GOOD, source_timestamp=None):
        self.vtype, self.value, self.status, self.source_timestamp = vtype, value, status, source_timestamp


class Item:
    def __init__(self, item_id, handle, node_id, sampling_ms, queue_size):
        self.id, self.handle, self.node_id = item_id, handle, node_id
        self.sampling, self.queue_size = max(sampling_ms, 10.0), max(1, min(queue_size, 100))
        self.queue, self.last, self.next_sample = [], None, 0.0


class Subscription:
    def __init__(self, sub_id, session, interval, lifetime, keepalive, max_notifications):
        self.id, self.session = sub_id, session
        self.max_notifications = max_notifications or 1000
        self.interval = max(interval, 20.0)
        self.keepalive = max(keepalive, 1)
        self.lifetime = max(lifetime, 3 * self.keepalive)
        self.items, self.sequence = {}, 1
        self.next_publish = time.monotonic() + self.interval / 1000
        self.keepalive_counter, self.lifetime_counter = 0, 0
        self.sent_first = False
        self.retransmit = {}
        self.expired = False


class Session:
    def __init__(self, session_id, token, conn, nonce, client_certificate, timeout_ms):
        self.id, self.token, self.conn = session_id, token, conn
        self.nonce, self.client_certificate = nonce, client_certificate
        self.timeout = timeout_ms / 1000
        self.activated, self.user = False, None
        self.publish_queue = []                  # (deadline, request_id, request_handle)
        self.last_activity = time.monotonic()


class Simulator:
    def __init__(self, host="127.0.0.1", port=0, endpoints=(("None", "None"),), certificate=None, private_key=None,
                 trusted_certificates=(), users=None, allow_anonymous=True, user_token_policy=None):
        self.certificate, self.private_key = certificate, private_key
        self.trusted = set(trusted_certificates)
        self.users = dict(users or {})
        self.allow_anonymous, self.user_token_policy = allow_anonymous, user_token_policy
        self.endpoint_list = [(POLICIES[p], MODES[m]) for p, m in endpoints]
        self.nodes, self.lock = {}, threading.RLock()
        self.sessions, self.subscriptions = {}, {}
        self.connections = set()
        self.ids = itertools.count(1)
        self.channel_ids, self.token_ids = itertools.count(1), itertools.count(1)
        self.corrupt_next, self.stopped = None, False
        self.counters = {"sessions": 0, "activations_rejected": 0, "channels_rejected": 0, "publish_responses": 0}
        self.server = socket.create_server((host, port))
        self.port = self.server.getsockname()[1]
        self.url = f"opc.tcp://{host}:{self.port}"
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._engine, daemon=True).start()

    # -- address space -------------------------------------------------------
    def add_variable(self, node_id, vtype, value):
        with self.lock:
            self.nodes[ua.NodeId.parse(node_id) if isinstance(node_id, str) else node_id] = Node(vtype, value)

    def set_value(self, node_id, value, status=ua.GOOD, vtype=None, source_timestamp=None):
        with self.lock:
            node = self.nodes[ua.NodeId.parse(node_id)]
            node.value, node.status = value, status
            node.vtype = vtype or node.vtype
            node.source_timestamp = source_timestamp

    def tick(self):
        """Deterministic generator: ns=2;s=Counter increments by one."""
        with self.lock:
            node = self.nodes.get(ua.NodeId(2, "Counter"))
            if node is not None:
                node.value += 1

    def _datavalue(self, node, now):
        if node is None:
            return ua.DataValue(status=ua.STATUS["BadNodeIdUnknown"], server_timestamp=now)
        variant = ua.Variant(node.vtype, node.value) if node.value is not None else None
        return ua.DataValue(variant, node.status, node.source_timestamp or now, now)

    # -- faults --------------------------------------------------------------
    def drop_connections(self):
        with self.lock:
            connections = list(self.connections)
        for conn in connections:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def expire_subscriptions(self):
        """Delete every subscription as if its lifetime counter had run out."""
        with self.lock:
            for sub in self.subscriptions.values():
                sub.expired = True

    def restart(self):
        """Forget all sessions and subscriptions and drop every connection."""
        with self.lock:
            self.sessions.clear()
            self.subscriptions.clear()
        self.drop_connections()

    def close(self):
        self.stopped = True
        try:
            self.server.shutdown(socket.SHUT_RDWR)     # wakes the blocked accept() so the port really closes
        except OSError:
            pass
        self.server.close()
        self.drop_connections()

    # -- connections -----------------------------------------------------------
    def _accept(self):
        while not self.stopped:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _trust(self, policy_uri, sender):
        if policy_uri == POLICY_NONE:                  # discovery is always reachable (OPC 10000-4 5.4)
            return Security(POLICY_NONE, MODE_NONE)
        if policy_uri not in {p for p, _ in self.endpoint_list}:
            raise SecurityError("security policy not offered", ua.STATUS["BadSecurityPolicyRejected"])
        if sender not in self.trusted:
            with self.lock:
                self.counters["channels_rejected"] += 1
            raise SecurityError("client certificate is not trusted", ua.STATUS["BadCertificateUntrusted"])
        # The mode arrives in the request body; Sign is assumed here and corrected after decoding.
        return Security(policy_uri, 2, self.private_key, self.certificate, sender)

    def _serve(self, sock):
        conn = Connection(sock)
        with self.lock:
            self.connections.add(conn)
        try:
            conn.accept_hello(time.monotonic() + 10)
            while not self.stopped:
                kind, request_id, body = conn.read_message(trust=self._trust if conn.keys is None else None)
                if kind == "OPN":
                    self._open(conn, request_id, body)
                elif kind == "CLO":
                    return
                else:
                    self._dispatch(conn, request_id, body)
        except SecurityError as error:
            conn.send_error(error.status, str(error))
        except ProtocolError as error:
            if error.status != ua.STATUS["BadSecureChannelClosed"]:
                conn.send_error(error.status, str(error))
        except ua.DecodeError as error:
            conn.send_error(ua.STATUS["BadDecodingError"], str(error))
        except OSError:
            pass
        finally:
            with self.lock:
                self.connections.discard(conn)
                for session in self.sessions.values():
                    if session.conn is conn:
                        session.conn, session.publish_queue = None, []
            conn.close()

    def _open(self, conn, request_id, body):
        channel_id, policy, _sender, payload = body
        name, request = ua.decode_message(payload)
        if name != "OpenSecureChannelRequest":
            raise ProtocolError("expected OpenSecureChannelRequest", ua.STATUS["BadDecodingError"])
        mode = request["security_mode"]
        if (policy, mode) not in self.endpoint_list and (policy, mode) != (POLICY_NONE, MODE_NONE):
            raise SecurityError("security mode not offered", ua.STATUS["BadSecurityPolicyRejected"])
        renew = request["request_type"] == 1
        if renew != (conn.keys is not None) or (renew and channel_id != conn.channel_id):
            raise SecurityError("invalid OpenSecureChannel request type")
        conn.security.mode = mode
        client_nonce = request["client_nonce"] or b""
        if conn.security.secure and len(client_nonce) != 32:
            raise SecurityError("client nonce length", ua.STATUS["BadNonceInvalid"])
        server_nonce = new_nonce() if conn.security.secure else b""
        if not renew:
            conn.channel_id = next(self.channel_ids)
        token_id = next(self.token_ids)
        lifetime = max(10000, min(request["requested_lifetime"], 3600000))
        response = ua.make("OpenSecureChannelResponse",
                           response_header=self._response_header(request),
                           security_token=ua.make("ChannelSecurityToken", channel_id=conn.channel_id, token_id=token_id,
                                                  created_at=ua.now_datetime(), revised_lifetime=lifetime),
                           server_nonce=server_nonce)
        conn.send_open(request_id, ua.encode_message("OpenSecureChannelResponse", response))
        conn.install_keys(token_id, client_nonce, server_nonce, False, activate=not renew)

    @staticmethod
    def _response_header(request, status=ua.GOOD):
        return ua.make("ResponseHeader", timestamp=ua.now_datetime(),
                       request_handle=request["request_header"]["request_handle"], service_result=status)

    def _send(self, conn, request_id, name, response):
        body = ua.encode_message(name, response)
        corrupt, self.corrupt_next = self.corrupt_next, None
        if corrupt == "chunk":
            conn.send_raw(b"XYZF" + struct.pack("<I", 12) + b"\0\0\0\0")
            return
        if corrupt == "body":
            body = body[:-3] + b"\x3f\xff\xff"      # invalid trailing bytes / variant type
        conn.send_message(b"MSG", request_id, body)

    def _fault(self, conn, request_id, request, status):
        self._send(conn, request_id, "ServiceFault", ua.make("ServiceFault",
                                                             response_header=self._response_header(request, status)))

    def _endpoints(self):
        endpoints = []
        for policy, mode in self.endpoint_list:
            tokens = []
            if self.allow_anonymous:
                tokens.append(ua.make("UserTokenPolicy", policy_id="anonymous", token_type=0))
            if self.users:
                tokens.append(ua.make("UserTokenPolicy", policy_id="username", token_type=1,
                                      security_policy_uri=self.user_token_policy))
            endpoints.append(ua.make(
                "EndpointDescription", endpoint_url=self.url,
                server=ua.make("ApplicationDescription", application_uri=SERVER_URI, product_uri="urn:suifleet",
                               application_name="SuiFleet OPC UA simulator", application_type=0,
                               discovery_urls=[self.url]),
                server_certificate=self.certificate if policy != POLICY_NONE or self.user_token_policy else None,
                security_mode=mode, security_policy_uri=policy, user_identity_tokens=tokens,
                transport_profile_uri=TRANSPORT_PROFILE, security_level=0 if policy == POLICY_NONE else 3))
        return endpoints

    def _dispatch(self, conn, request_id, payload):
        name, request = ua.decode_message(payload)
        if name == "GetEndpointsRequest":
            return self._send(conn, request_id, "GetEndpointsResponse", ua.make(
                "GetEndpointsResponse", response_header=self._response_header(request), endpoints=self._endpoints()))
        if (conn.security.policy_uri, conn.security.mode) not in self.endpoint_list:
            return self._fault(conn, request_id, request, ua.STATUS["BadSecurityPolicyRejected"])
        if name == "CreateSessionRequest":
            return self._create_session(conn, request_id, request)
        with self.lock:
            session = self.sessions.get(request["request_header"]["authentication_token"])
        if session is None or session.conn not in (conn, None):
            return self._fault(conn, request_id, request, ua.STATUS["BadSessionIdInvalid"])
        session.last_activity = time.monotonic()
        if name == "ActivateSessionRequest":
            return self._activate(conn, request_id, request, session)
        if not session.activated:
            return self._fault(conn, request_id, request, ua.STATUS["BadSessionNotActivated"])
        if name == "CloseSessionRequest":
            with self.lock:
                self.sessions.pop(session.token, None)
                for sub_id in [s for s, sub in self.subscriptions.items() if sub.session is session]:
                    del self.subscriptions[sub_id]
            return self._send(conn, request_id, "CloseSessionResponse", ua.make(
                "CloseSessionResponse", response_header=self._response_header(request)))
        if name == "ReadRequest":
            now = ua.now_datetime()
            with self.lock:
                results = [self._datavalue(self.nodes.get(r["node_id"]), now) if r["attribute_id"] == 13
                           else ua.DataValue(status=ua.STATUS["BadAttributeIdInvalid"])
                           for r in request["nodes_to_read"]]
            return self._send(conn, request_id, "ReadResponse", ua.make(
                "ReadResponse", response_header=self._response_header(request), results=results))
        if name == "CreateSubscriptionRequest":
            with self.lock:
                sub = Subscription(next(self.ids), session, request["requested_publishing_interval"],
                                   request["requested_lifetime_count"], request["requested_max_keep_alive_count"],
                                   request["max_notifications_per_publish"])
                self.subscriptions[sub.id] = sub
            return self._send(conn, request_id, "CreateSubscriptionResponse", ua.make(
                "CreateSubscriptionResponse", response_header=self._response_header(request), subscription_id=sub.id,
                revised_publishing_interval=sub.interval, revised_lifetime_count=sub.lifetime,
                revised_max_keep_alive_count=sub.keepalive))
        if name == "CreateMonitoredItemsRequest":
            return self._create_items(conn, request_id, request, session)
        if name == "PublishRequest":
            return self._publish_request(conn, request_id, request, session)
        if name == "DeleteSubscriptionsRequest":
            results = []
            with self.lock:
                for sub_id in request["subscription_ids"]:
                    sub = self.subscriptions.get(sub_id)
                    if sub is not None and sub.session is session:
                        del self.subscriptions[sub_id]
                        results.append(ua.GOOD)
                    else:
                        results.append(ua.STATUS["BadSubscriptionIdInvalid"])
            return self._send(conn, request_id, "DeleteSubscriptionsResponse", ua.make(
                "DeleteSubscriptionsResponse", response_header=self._response_header(request), results=results))
        return self._fault(conn, request_id, request, ua.STATUS["BadServiceUnsupported"])

    def _create_session(self, conn, request_id, request):
        secure = conn.security.secure
        client_certificate = request["client_certificate"]
        if secure and client_certificate != conn.security.peer_certificate:
            return self._fault(conn, request_id, request, ua.STATUS["BadCertificateInvalid"])
        nonce = new_nonce()
        with self.lock:
            session = Session(ua.NodeId(1, next(self.ids)), ua.NodeId(1, f"token-{next(self.ids)}-{nonce[:8].hex()}"),
                              conn, nonce, client_certificate, max(10000, min(request["requested_session_timeout"], 3600000)))
            self.sessions[session.token] = session
            self.counters["sessions"] += 1
        signature = ua.make("SignatureData")
        if secure:
            signature = ua.make("SignatureData", algorithm=RSA_SHA256,
                                signature=conn.security.sign(client_certificate + (request["client_nonce"] or b"")))
        self._send(conn, request_id, "CreateSessionResponse", ua.make(
            "CreateSessionResponse", response_header=self._response_header(request), session_id=session.id,
            authentication_token=session.token, revised_session_timeout=session.timeout * 1000, server_nonce=nonce,
            server_certificate=self.certificate if secure or self.user_token_policy else None,
            server_endpoints=self._endpoints(), server_signature=signature, max_request_message_size=0))

    def _activate(self, conn, request_id, request, session):
        def reject(status):
            with self.lock:
                self.counters["activations_rejected"] += 1
            return self._fault(conn, request_id, request, status)
        if conn.security.secure:
            signature = request["client_signature"]
            if signature["algorithm"] != RSA_SHA256 or not Security.verify(
                    session.client_certificate, signature["signature"] or b"", self.certificate + session.nonce):
                return reject(ua.STATUS["BadApplicationSignatureInvalid"])
        token = request["user_identity_token"]
        type_id = token.type_id.identifier if token is not None else None
        if type_id == ua.ENCODING_IDS["AnonymousIdentityToken"]:
            if not self.allow_anonymous or token.body.get("policy_id") != "anonymous":
                return reject(ua.STATUS["BadIdentityTokenRejected"])
            user = None
        elif type_id == ua.ENCODING_IDS["UserNameIdentityToken"] and self.users:
            body = token.body
            if body["policy_id"] != "username":
                return reject(ua.STATUS["BadIdentityTokenRejected"])
            policy = self.user_token_policy or conn.security.policy_uri
            try:
                if policy == POLICY_NONE:
                    if body["encryption_algorithm"]:
                        return reject(ua.STATUS["BadIdentityTokenInvalid"])
                    password = body["password"] or b""
                else:
                    if body["encryption_algorithm"] != RSA_OAEP:
                        return reject(ua.STATUS["BadIdentityTokenInvalid"])
                    password = Security(policy, 2, self.private_key, self.certificate).decrypt_secret(
                        body["password"] or b"", session.nonce)
            except SecurityError:
                return reject(ua.STATUS["BadIdentityTokenInvalid"])
            expected = self.users.get(body["user_name"])
            if expected is None or not hmac.compare_digest(expected.encode(), password):
                return reject(ua.STATUS["BadUserAccessDenied"])
            user = body["user_name"]
        else:
            return reject(ua.STATUS["BadIdentityTokenInvalid"])
        session.nonce, session.activated, session.user, session.conn = new_nonce(), True, user, conn
        self._send(conn, request_id, "ActivateSessionResponse", ua.make(
            "ActivateSessionResponse", response_header=self._response_header(request), server_nonce=session.nonce))

    def _create_items(self, conn, request_id, request, session):
        results = []
        with self.lock:
            sub = self.subscriptions.get(request["subscription_id"])
            if sub is None or sub.session is not session:
                return self._fault(conn, request_id, request, ua.STATUS["BadSubscriptionIdInvalid"])
            for create in request["items_to_create"]:
                node_id = create["item_to_monitor"]["node_id"]
                params = create["requested_parameters"]
                if node_id not in self.nodes:
                    results.append(ua.make("MonitoredItemCreateResult", status_code=ua.STATUS["BadNodeIdUnknown"]))
                    continue
                item = Item(next(self.ids), params["client_handle"], node_id, params["sampling_interval"], params["queue_size"])
                sub.items[item.id] = item
                results.append(ua.make("MonitoredItemCreateResult", monitored_item_id=item.id,
                                       revised_sampling_interval=item.sampling, revised_queue_size=item.queue_size))
        self._send(conn, request_id, "CreateMonitoredItemsResponse", ua.make(
            "CreateMonitoredItemsResponse", response_header=self._response_header(request), results=results))

    def _publish_request(self, conn, request_id, request, session):
        with self.lock:
            results = []
            for ack in request["subscription_acknowledgements"]:
                sub = self.subscriptions.get(ack["subscription_id"])
                ok = sub is not None and sub.retransmit.pop(ack["sequence_number"], None) is not None
                results.append(ua.GOOD if ok else 0x807A0000)            # BadSequenceNumberUnknown
            if not any(sub.session is session for sub in self.subscriptions.values()):
                return self._fault(conn, request_id, request, ua.STATUS["BadNoSubscription"])
            if len(session.publish_queue) >= 10:
                return self._fault(conn, request_id, request, ua.STATUS["BadTooManyPublishRequests"])
            for sub in self.subscriptions.values():
                if sub.session is session:
                    sub.lifetime_counter = 0
            timeout = request["request_header"]["timeout_hint"] / 1000 or 60
            session.publish_queue.append((time.monotonic() + timeout, request_id, request, results))

    # -- publishing engine -------------------------------------------------------
    def _engine(self):
        while not self.stopped:
            time.sleep(0.005)
            now_mono = time.monotonic()
            outgoing = []
            with self.lock:
                now = ua.now_datetime()
                for session in self.sessions.values():
                    for entry in [e for e in session.publish_queue if e[0] < now_mono]:
                        session.publish_queue.remove(entry)
                        outgoing.append((session.conn, entry[1], "ServiceFault", ua.make(
                            "ServiceFault", response_header=self._response_header(entry[2], ua.STATUS["BadTimeout"]))))
                for sub in list(self.subscriptions.values()):
                    for item in sub.items.values():
                        if now_mono >= item.next_sample:
                            item.next_sample = now_mono + item.sampling / 1000
                            self._sample(item, now)
                    if now_mono < sub.next_publish:
                        continue
                    sub.next_publish = now_mono + sub.interval / 1000
                    message = self._service(sub, now, now_mono)
                    if message is not None:
                        outgoing.append(message)
            for conn, request_id, name, response in outgoing:
                if conn is None:
                    continue
                try:
                    self._send(conn, request_id, name, response)
                    self.counters["publish_responses"] += name == "PublishResponse"
                except (ProtocolError, OSError):
                    pass

    def _sample(self, item, now):
        node = self.nodes.get(item.node_id)
        current = (node.vtype, node.value, node.status, node.source_timestamp) if node else None
        if current == item.last:
            return
        item.last = current
        item.queue.append(self._datavalue(node, now))
        if len(item.queue) > item.queue_size:
            del item.queue[0]
            item.queue[0].status = (item.queue[0].status or 0) | ua.OVERFLOW_BITS

    def _service(self, sub, now, now_mono):
        """One publishing cycle (OPC 10000-4 5.13.1.2 state table, simplified)."""
        session = sub.session
        if sub.expired:
            del self.subscriptions[sub.id]
            if session.publish_queue and session.conn is not None:
                _, request_id, request, results = session.publish_queue.pop(0)
                status = ua.ExtensionObject(ua.NodeId(0, ua.ENCODING_IDS["StatusChangeNotification"]),
                                            {"status": ua.STATUS["BadTimeout"], "diagnostic_info": None})
                return (session.conn, request_id, "PublishResponse", ua.make(
                    "PublishResponse", response_header=self._response_header(request), subscription_id=sub.id,
                    notification_message=ua.make("NotificationMessage", sequence_number=sub.sequence, publish_time=now,
                                                 notification_data=[status]), results=results))
            return None
        pending = [(item.handle, dv) for item in sub.items.values() for dv in item.queue]
        if not pending:
            sub.keepalive_counter += 1
            if sub.keepalive_counter < sub.keepalive and sub.sent_first:
                if session.publish_queue:
                    sub.lifetime_counter = 0
                    return None
                return self._lifetime(sub)
        if not session.publish_queue or session.conn is None:
            return self._lifetime(sub)
        sub.lifetime_counter = sub.keepalive_counter = 0
        sub.sent_first = True
        _, request_id, request, results = session.publish_queue.pop(0)
        data, sequence, more = [], sub.sequence, len(pending) > sub.max_notifications
        if pending:
            pending = pending[:sub.max_notifications]
            taken = len(pending)
            for item in sub.items.values():
                n = min(taken, len(item.queue))
                item.queue, taken = item.queue[n:], taken - n
            data = [ua.ExtensionObject(ua.NodeId(0, ua.ENCODING_IDS["DataChangeNotification"]), {
                "monitored_items": [{"client_handle": h, "value": dv} for h, dv in pending], "diagnostic_infos": []})]
            sub.retransmit[sequence] = True
            while len(sub.retransmit) > 64:
                del sub.retransmit[min(sub.retransmit)]
            sub.sequence += 1
        return (session.conn, request_id, "PublishResponse", ua.make(
            "PublishResponse", response_header=self._response_header(request), subscription_id=sub.id,
            available_sequence_numbers=sorted(sub.retransmit), more_notifications=more,
            notification_message=ua.make("NotificationMessage", sequence_number=sequence, publish_time=now,
                                         notification_data=data), results=results))

    def _lifetime(self, sub):
        sub.lifetime_counter += 1
        if sub.lifetime_counter >= sub.lifetime:
            del self.subscriptions[sub.id]          # no Publish requests for a whole lifetime
        return None
