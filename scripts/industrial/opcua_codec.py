"""Clean-room OPC UA binary encoding subset (SF-070). Stdlib only.

Implemented from OPC 10000-6 (Mappings) 5.2 "OPC UA Binary" and the service
parameter tables of OPC 10000-4 (Services), release 1.05. Only the structures
used by a data-access client and a small simulator are defined. Decoding is
bounded: lengths are checked against the remaining buffer, arrays against
MAX_ARRAY and nesting against MAX_DEPTH, so malformed input raises DecodeError
instead of allocating or recursing without limit.
"""
import datetime
import struct
import uuid

MAX_ARRAY = 10000
MAX_DEPTH = 8
EPOCH_OFFSET_100NS = 116444736000000000        # 1601-01-01 to 1970-01-01 in 100 ns units


class DecodeError(ValueError):
    pass


# Built-in type identifiers (OPC 10000-6 5.1.2).
BOOLEAN, SBYTE, BYTE, INT16, UINT16, INT32, UINT32, INT64, UINT64, FLOAT, DOUBLE, STRING, DATETIME, GUID, \
    BYTESTRING, XMLELEMENT, NODEID, EXPANDEDNODEID, STATUSCODE, QUALIFIEDNAME, LOCALIZEDTEXT, EXTENSIONOBJECT, \
    DATAVALUE, VARIANT, DIAGNOSTICINFO = range(1, 26)
VARIANT_NAMES = {BOOLEAN: "boolean", SBYTE: "sbyte", BYTE: "byte", INT16: "int16", UINT16: "uint16", INT32: "int32",
                 UINT32: "uint32", INT64: "int64", UINT64: "uint64", FLOAT: "float", DOUBLE: "double",
                 STRING: "string", DATETIME: "datetime", BYTESTRING: "bytestring", STATUSCODE: "statuscode"}

SCALARS = {"Boolean": "<?", "SByte": "<b", "Byte": "<B", "Int16": "<h", "UInt16": "<H", "Int32": "<i",
           "UInt32": "<I", "Int64": "<q", "UInt64": "<Q", "Float": "<f", "Double": "<d", "StatusCode": "<I",
           "DateTime": "<q"}


class NodeId:
    __slots__ = ("namespace", "identifier")

    def __init__(self, namespace=0, identifier=0):
        self.namespace, self.identifier = namespace, identifier

    def __eq__(self, other):
        return isinstance(other, NodeId) and (self.namespace, self.identifier) == (other.namespace, other.identifier)

    def __hash__(self):
        return hash((self.namespace, self.identifier))

    def __repr__(self):
        return f"NodeId({self})"

    def __str__(self):
        prefix = f"ns={self.namespace};" if self.namespace else ""
        i = self.identifier
        if isinstance(i, int):
            return f"{prefix}i={i}"
        if isinstance(i, str):
            return f"{prefix}s={i}"
        if isinstance(i, uuid.UUID):
            return f"{prefix}g={i}"
        return f"{prefix}b={i.hex()}"

    @classmethod
    def parse(cls, text):
        """Parse the standard string form, e.g. 'ns=2;s=Line1.Temperature' or 'i=2258'."""
        namespace, rest = 0, text
        if text.startswith("ns="):
            head, sep, rest = text.partition(";")
            if not sep or not head[3:].isdigit():
                raise ValueError(f"invalid NodeId {text!r}")
            namespace = int(head[3:])
        kind, value = rest[:2], rest[2:]
        if namespace > 0xFFFF:
            raise ValueError("namespace index out of range")
        if kind == "i=" and value.isdigit() and int(value) <= 0xFFFFFFFF:
            return cls(namespace, int(value))
        if kind == "s=" and value and len(value.encode()) <= 4096:
            return cls(namespace, value)
        if kind == "g=":
            return cls(namespace, uuid.UUID(value))
        if kind == "b=":
            return cls(namespace, bytes.fromhex(value))
        raise ValueError(f"invalid NodeId {text!r}")


class Variant:
    __slots__ = ("type", "value")

    def __init__(self, vtype, value):
        self.type, self.value = vtype, value

    def __eq__(self, other):
        return isinstance(other, Variant) and (self.type, self.value) == (other.type, other.value)

    def __repr__(self):
        return f"Variant({VARIANT_NAMES.get(self.type, self.type)}, {self.value!r})"


class ExtensionObject:
    __slots__ = ("type_id", "body")

    def __init__(self, type_id=None, body=None):
        self.type_id, self.body = type_id or NodeId(), body


class DataValue:
    __slots__ = ("value", "status", "source_timestamp", "server_timestamp")

    def __init__(self, value=None, status=None, source_timestamp=None, server_timestamp=None):
        self.value, self.status = value, status
        self.source_timestamp, self.server_timestamp = source_timestamp, server_timestamp


def datetime_to_ns(value):
    """OPC UA DateTime (100 ns since 1601) -> ns since the Unix epoch, or None."""
    if value is None or value <= 0 or value == 0x7FFFFFFFFFFFFFFF:   # absent, 0 or Int64.Max: no time (5.2.2.5)
        return None
    return (value - EPOCH_OFFSET_100NS) * 100


def ns_to_datetime(ns):
    return ns // 100 + EPOCH_OFFSET_100NS


def now_datetime():
    return ns_to_datetime(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1e9))


class Writer:
    def __init__(self):
        self.parts = []

    def raw(self, data):
        self.parts.append(bytes(data))

    def scalar(self, kind, value):
        self.parts.append(struct.pack(SCALARS[kind], value))

    def bytes(self):
        return b"".join(self.parts)


class Reader:
    def __init__(self, data, offset=0):
        self.data, self.offset, self.depth = data, offset, 0

    def take(self, n):
        if n < 0 or self.offset + n > len(self.data):
            raise DecodeError("truncated")
        out = self.data[self.offset:self.offset + n]
        self.offset += n
        return out

    def scalar(self, kind):
        fmt = SCALARS[kind]
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def remaining(self):
        return len(self.data) - self.offset


# --- Built-in types ------------------------------------------------------------

def put_string(w, value):
    if value is None:
        w.scalar("Int32", -1)
        return
    data = value.encode() if isinstance(value, str) else bytes(value)
    w.scalar("Int32", len(data))
    w.raw(data)


def get_bytes(r):
    n = r.scalar("Int32")
    if n == -1:
        return None
    if n < -1:
        raise DecodeError("negative length")
    return r.take(n)


def get_string(r):
    data = get_bytes(r)
    if data is None:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise DecodeError("invalid UTF-8") from None


def put_guid(w, value):
    w.raw(value.bytes_le)


def get_guid(r):
    return uuid.UUID(bytes_le=r.take(16))


def put_nodeid(w, node):
    ns, i = node.namespace, node.identifier
    if isinstance(i, int):
        if ns == 0 and i <= 0xFF:
            w.raw(bytes([0, i]))
        elif ns <= 0xFF and i <= 0xFFFF:
            w.raw(struct.pack("<BBH", 1, ns, i))
        else:
            w.raw(struct.pack("<BHI", 2, ns, i))
    elif isinstance(i, str):
        w.raw(struct.pack("<BH", 3, ns))
        put_string(w, i)
    elif isinstance(i, uuid.UUID):
        w.raw(struct.pack("<BH", 4, ns))
        put_guid(w, i)
    else:
        w.raw(struct.pack("<BH", 5, ns))
        put_string(w, i)


def get_nodeid(r, expanded=False):
    encoding = r.scalar("Byte")
    flags, kind = encoding & 0xC0, encoding & 0x3F
    if flags and not expanded:
        raise DecodeError("NodeId flags")
    if kind == 0:
        node = NodeId(0, r.scalar("Byte"))
    elif kind == 1:
        node = NodeId(r.scalar("Byte"), r.scalar("UInt16"))
    elif kind == 2:
        node = NodeId(r.scalar("UInt16"), r.scalar("UInt32"))
    elif kind == 3:
        ns = r.scalar("UInt16")
        node = NodeId(ns, get_string(r) or "")
    elif kind == 4:
        node = NodeId(r.scalar("UInt16"), get_guid(r))
    elif kind == 5:
        ns = r.scalar("UInt16")
        node = NodeId(ns, get_bytes(r) or b"")
    else:
        raise DecodeError("NodeId encoding")
    if flags & 0x80:
        get_string(r)                      # namespace URI: not used by this subset
    if flags & 0x40:
        r.scalar("UInt32")                 # server index
    return node


def put_localizedtext(w, text):
    if text is None:
        w.raw(b"\x00")
    else:
        w.raw(b"\x02")
        put_string(w, text)


def get_localizedtext(r):
    mask = r.scalar("Byte")
    if mask & ~0x03:
        raise DecodeError("LocalizedText mask")
    if mask & 1:
        get_string(r)
    return get_string(r) if mask & 2 else None


def put_qualifiedname(w, value):
    ns, name = value if value else (0, None)
    w.scalar("UInt16", ns)
    put_string(w, name)


def get_qualifiedname(r):
    return (r.scalar("UInt16"), get_string(r))


def get_diagnosticinfo(r):
    r.depth += 1
    if r.depth > MAX_DEPTH:
        raise DecodeError("DiagnosticInfo nesting")
    mask = r.scalar("Byte")
    for bit in (0x01, 0x02, 0x04, 0x08):
        if mask & bit:
            r.scalar("Int32")
    if mask & 0x10:
        get_string(r)
    if mask & 0x20:
        r.scalar("UInt32")
    if mask & 0x40:
        get_diagnosticinfo(r)
    r.depth -= 1
    return None


def put_diagnosticinfo(w, _value):
    w.raw(b"\x00")


def put_extensionobject(w, obj):
    if obj is None or obj.body is None:
        put_nodeid(w, obj.type_id if obj else NodeId())
        w.raw(b"\x00")
        return
    put_nodeid(w, obj.type_id)
    w.raw(b"\x01")
    body = obj.body
    if not isinstance(body, (bytes, bytearray)):
        body = encode_struct(STRUCT_BY_ID[obj.type_id.identifier], body)
    put_string(w, body)


def get_extensionobject(r):
    type_id = get_nodeid(r)
    encoding = r.scalar("Byte")
    if encoding == 0:
        return ExtensionObject(type_id, None)
    if encoding not in (1, 2):
        raise DecodeError("ExtensionObject encoding")
    body = get_bytes(r) or b""
    name = STRUCT_BY_ID.get(type_id.identifier) if type_id.namespace == 0 and encoding == 1 else None
    if name is None:
        return ExtensionObject(type_id, body)
    inner = Reader(body)
    inner.depth = r.depth + 1
    if inner.depth > MAX_DEPTH:
        raise DecodeError("ExtensionObject nesting")
    value = decode_struct(name, inner)
    if inner.remaining():
        raise DecodeError("trailing ExtensionObject bytes")
    return ExtensionObject(type_id, value)


VARIANT_CODECS = {
    BOOLEAN: ("Boolean",), SBYTE: ("SByte",), BYTE: ("Byte",), INT16: ("Int16",), UINT16: ("UInt16",),
    INT32: ("Int32",), UINT32: ("UInt32",), INT64: ("Int64",), UINT64: ("UInt64",), FLOAT: ("Float",),
    DOUBLE: ("Double",), DATETIME: ("DateTime",), STATUSCODE: ("StatusCode",),
}


def _put_variant_element(w, vtype, value):
    if vtype in VARIANT_CODECS:
        w.scalar(VARIANT_CODECS[vtype][0], value)
    elif vtype in (STRING, XMLELEMENT, BYTESTRING):
        put_string(w, value)
    elif vtype == GUID:
        put_guid(w, value)
    elif vtype == NODEID:
        put_nodeid(w, value)
    elif vtype == QUALIFIEDNAME:
        put_qualifiedname(w, value)
    elif vtype == LOCALIZEDTEXT:
        put_localizedtext(w, value)
    elif vtype == EXTENSIONOBJECT:
        put_extensionobject(w, value)
    elif vtype == DATAVALUE:
        put_datavalue(w, value)
    elif vtype == VARIANT:
        put_variant(w, value)
    else:
        raise ValueError(f"cannot encode variant type {vtype}")


def _get_variant_element(r, vtype):
    if vtype in VARIANT_CODECS:
        return r.scalar(VARIANT_CODECS[vtype][0])
    if vtype == STRING or vtype == XMLELEMENT:
        return get_string(r)
    if vtype == BYTESTRING:
        return get_bytes(r)
    if vtype == GUID:
        return get_guid(r)
    if vtype == NODEID:
        return get_nodeid(r)
    if vtype == EXPANDEDNODEID:
        return get_nodeid(r, expanded=True)
    if vtype == QUALIFIEDNAME:
        return get_qualifiedname(r)
    if vtype == LOCALIZEDTEXT:
        return get_localizedtext(r)
    if vtype == EXTENSIONOBJECT:
        return get_extensionobject(r)
    if vtype == DATAVALUE:
        return get_datavalue(r)
    if vtype == VARIANT:
        return get_variant(r)
    if vtype == DIAGNOSTICINFO:
        return get_diagnosticinfo(r)
    raise DecodeError(f"variant type {vtype}")


def put_variant(w, variant):
    if variant is None:
        w.raw(b"\x00")
        return
    if isinstance(variant.value, list):
        w.raw(bytes([variant.type | 0x80]))
        w.scalar("Int32", len(variant.value))
        for item in variant.value:
            _put_variant_element(w, variant.type, item)
    else:
        w.raw(bytes([variant.type]))
        _put_variant_element(w, variant.type, variant.value)


def get_variant(r):
    r.depth += 1
    if r.depth > MAX_DEPTH:
        raise DecodeError("Variant nesting")
    mask = r.scalar("Byte")
    vtype = mask & 0x3F
    if vtype == 0:
        r.depth -= 1
        return None
    if vtype > 25:
        raise DecodeError("variant type")
    if mask & 0x80:
        n = r.scalar("Int32")
        if n > MAX_ARRAY or n < -1:
            raise DecodeError("array length")
        value = [] if n == -1 else [_get_variant_element(r, vtype) for _ in range(n)]
        if mask & 0x40:
            dims = r.scalar("Int32")
            if not 0 <= dims <= 32:
                raise DecodeError("array dimensions")
            for _ in range(dims):
                r.scalar("Int32")
    else:
        if mask & 0x40:
            raise DecodeError("dimensions without array")
        value = _get_variant_element(r, vtype)
    r.depth -= 1
    return Variant(vtype, value)


def put_datavalue(w, dv):
    mask = (0x01 if dv.value is not None else 0) | (0x02 if dv.status else 0) | \
        (0x04 if dv.source_timestamp else 0) | (0x08 if dv.server_timestamp else 0)
    w.raw(bytes([mask]))
    if mask & 0x01:
        put_variant(w, dv.value)
    if mask & 0x02:
        w.scalar("StatusCode", dv.status)
    if mask & 0x04:
        w.scalar("DateTime", dv.source_timestamp)
    if mask & 0x08:
        w.scalar("DateTime", dv.server_timestamp)


def get_datavalue(r):
    mask = r.scalar("Byte")
    if mask & 0xC0:
        raise DecodeError("DataValue mask")
    dv = DataValue()
    if mask & 0x01:
        dv.value = get_variant(r)
    dv.status = r.scalar("StatusCode") if mask & 0x02 else 0
    if mask & 0x04:
        dv.source_timestamp = r.scalar("DateTime")
    if mask & 0x10:
        r.scalar("UInt16")
    if mask & 0x08:
        dv.server_timestamp = r.scalar("DateTime")
    if mask & 0x20:
        r.scalar("UInt16")
    return dv


BUILTIN = {
    "String": (put_string, get_string), "ByteString": (put_string, get_bytes),
    "NodeId": (put_nodeid, get_nodeid), "LocalizedText": (put_localizedtext, get_localizedtext),
    "QualifiedName": (put_qualifiedname, get_qualifiedname), "DiagnosticInfo": (put_diagnosticinfo, get_diagnosticinfo),
    "ExtensionObject": (put_extensionobject, get_extensionobject), "Variant": (put_variant, get_variant),
    "DataValue": (put_datavalue, get_datavalue),
}

# --- Structures (OPC 10000-4 parameter tables) -----------------------------------
# Fields are (name, type); "T[]" is an Int32-prefixed array. Enumerations are UInt32 on the wire.
STRUCTS = {
    "RequestHeader": [("authentication_token", "NodeId"), ("timestamp", "DateTime"), ("request_handle", "UInt32"),
                      ("return_diagnostics", "UInt32"), ("audit_entry_id", "String"), ("timeout_hint", "UInt32"),
                      ("additional_header", "ExtensionObject")],
    "ResponseHeader": [("timestamp", "DateTime"), ("request_handle", "UInt32"), ("service_result", "StatusCode"),
                       ("service_diagnostics", "DiagnosticInfo"), ("string_table", "String[]"),
                       ("additional_header", "ExtensionObject")],
    "ServiceFault": [("response_header", "ResponseHeader")],
    "ChannelSecurityToken": [("channel_id", "UInt32"), ("token_id", "UInt32"), ("created_at", "DateTime"),
                             ("revised_lifetime", "UInt32")],
    "OpenSecureChannelRequest": [("request_header", "RequestHeader"), ("client_protocol_version", "UInt32"),
                                 ("request_type", "UInt32"), ("security_mode", "UInt32"), ("client_nonce", "ByteString"),
                                 ("requested_lifetime", "UInt32")],
    "OpenSecureChannelResponse": [("response_header", "ResponseHeader"), ("server_protocol_version", "UInt32"),
                                  ("security_token", "ChannelSecurityToken"), ("server_nonce", "ByteString")],
    "CloseSecureChannelRequest": [("request_header", "RequestHeader")],
    "ApplicationDescription": [("application_uri", "String"), ("product_uri", "String"),
                               ("application_name", "LocalizedText"), ("application_type", "UInt32"),
                               ("gateway_server_uri", "String"), ("discovery_profile_uri", "String"),
                               ("discovery_urls", "String[]")],
    "UserTokenPolicy": [("policy_id", "String"), ("token_type", "UInt32"), ("issued_token_type", "String"),
                        ("issuer_endpoint_url", "String"), ("security_policy_uri", "String")],
    "EndpointDescription": [("endpoint_url", "String"), ("server", "ApplicationDescription"),
                            ("server_certificate", "ByteString"), ("security_mode", "UInt32"),
                            ("security_policy_uri", "String"), ("user_identity_tokens", "UserTokenPolicy[]"),
                            ("transport_profile_uri", "String"), ("security_level", "Byte")],
    "GetEndpointsRequest": [("request_header", "RequestHeader"), ("endpoint_url", "String"), ("locale_ids", "String[]"),
                            ("profile_uris", "String[]")],
    "GetEndpointsResponse": [("response_header", "ResponseHeader"), ("endpoints", "EndpointDescription[]")],
    "SignedSoftwareCertificate": [("certificate_data", "ByteString"), ("signature", "ByteString")],
    "SignatureData": [("algorithm", "String"), ("signature", "ByteString")],
    "CreateSessionRequest": [("request_header", "RequestHeader"), ("client_description", "ApplicationDescription"),
                             ("server_uri", "String"), ("endpoint_url", "String"), ("session_name", "String"),
                             ("client_nonce", "ByteString"), ("client_certificate", "ByteString"),
                             ("requested_session_timeout", "Double"), ("max_response_message_size", "UInt32")],
    "CreateSessionResponse": [("response_header", "ResponseHeader"), ("session_id", "NodeId"),
                              ("authentication_token", "NodeId"), ("revised_session_timeout", "Double"),
                              ("server_nonce", "ByteString"), ("server_certificate", "ByteString"),
                              ("server_endpoints", "EndpointDescription[]"),
                              ("server_software_certificates", "SignedSoftwareCertificate[]"),
                              ("server_signature", "SignatureData"), ("max_request_message_size", "UInt32")],
    "AnonymousIdentityToken": [("policy_id", "String")],
    "UserNameIdentityToken": [("policy_id", "String"), ("user_name", "String"), ("password", "ByteString"),
                              ("encryption_algorithm", "String")],
    "ActivateSessionRequest": [("request_header", "RequestHeader"), ("client_signature", "SignatureData"),
                               ("client_software_certificates", "SignedSoftwareCertificate[]"),
                               ("locale_ids", "String[]"), ("user_identity_token", "ExtensionObject"),
                               ("user_token_signature", "SignatureData")],
    "ActivateSessionResponse": [("response_header", "ResponseHeader"), ("server_nonce", "ByteString"),
                                ("results", "StatusCode[]"), ("diagnostic_infos", "DiagnosticInfo[]")],
    "CloseSessionRequest": [("request_header", "RequestHeader"), ("delete_subscriptions", "Boolean")],
    "CloseSessionResponse": [("response_header", "ResponseHeader")],
    "ReadValueId": [("node_id", "NodeId"), ("attribute_id", "UInt32"), ("index_range", "String"),
                    ("data_encoding", "QualifiedName")],
    "ReadRequest": [("request_header", "RequestHeader"), ("max_age", "Double"), ("timestamps_to_return", "UInt32"),
                    ("nodes_to_read", "ReadValueId[]")],
    "ReadResponse": [("response_header", "ResponseHeader"), ("results", "DataValue[]"),
                     ("diagnostic_infos", "DiagnosticInfo[]")],
    "CreateSubscriptionRequest": [("request_header", "RequestHeader"), ("requested_publishing_interval", "Double"),
                                  ("requested_lifetime_count", "UInt32"), ("requested_max_keep_alive_count", "UInt32"),
                                  ("max_notifications_per_publish", "UInt32"), ("publishing_enabled", "Boolean"),
                                  ("priority", "Byte")],
    "CreateSubscriptionResponse": [("response_header", "ResponseHeader"), ("subscription_id", "UInt32"),
                                   ("revised_publishing_interval", "Double"), ("revised_lifetime_count", "UInt32"),
                                   ("revised_max_keep_alive_count", "UInt32")],
    "MonitoringParameters": [("client_handle", "UInt32"), ("sampling_interval", "Double"), ("filter", "ExtensionObject"),
                             ("queue_size", "UInt32"), ("discard_oldest", "Boolean")],
    "MonitoredItemCreateRequest": [("item_to_monitor", "ReadValueId"), ("monitoring_mode", "UInt32"),
                                   ("requested_parameters", "MonitoringParameters")],
    "CreateMonitoredItemsRequest": [("request_header", "RequestHeader"), ("subscription_id", "UInt32"),
                                    ("timestamps_to_return", "UInt32"), ("items_to_create", "MonitoredItemCreateRequest[]")],
    "MonitoredItemCreateResult": [("status_code", "StatusCode"), ("monitored_item_id", "UInt32"),
                                  ("revised_sampling_interval", "Double"), ("revised_queue_size", "UInt32"),
                                  ("filter_result", "ExtensionObject")],
    "CreateMonitoredItemsResponse": [("response_header", "ResponseHeader"), ("results", "MonitoredItemCreateResult[]"),
                                     ("diagnostic_infos", "DiagnosticInfo[]")],
    "SubscriptionAcknowledgement": [("subscription_id", "UInt32"), ("sequence_number", "UInt32")],
    "PublishRequest": [("request_header", "RequestHeader"),
                       ("subscription_acknowledgements", "SubscriptionAcknowledgement[]")],
    "NotificationMessage": [("sequence_number", "UInt32"), ("publish_time", "DateTime"),
                            ("notification_data", "ExtensionObject[]")],
    "PublishResponse": [("response_header", "ResponseHeader"), ("subscription_id", "UInt32"),
                        ("available_sequence_numbers", "UInt32[]"), ("more_notifications", "Boolean"),
                        ("notification_message", "NotificationMessage"), ("results", "StatusCode[]"),
                        ("diagnostic_infos", "DiagnosticInfo[]")],
    "MonitoredItemNotification": [("client_handle", "UInt32"), ("value", "DataValue")],
    "DataChangeNotification": [("monitored_items", "MonitoredItemNotification[]"),
                               ("diagnostic_infos", "DiagnosticInfo[]")],
    "StatusChangeNotification": [("status", "StatusCode"), ("diagnostic_info", "DiagnosticInfo")],
    "DeleteSubscriptionsRequest": [("request_header", "RequestHeader"), ("subscription_ids", "UInt32[]")],
    "DeleteSubscriptionsResponse": [("response_header", "ResponseHeader"), ("results", "StatusCode[]"),
                                    ("diagnostic_infos", "DiagnosticInfo[]")],
}

# DefaultBinary encoding NodeIds (namespace 0) from the OPC UA NodeSet.
ENCODING_IDS = {
    "ServiceFault": 397, "AnonymousIdentityToken": 321, "UserNameIdentityToken": 324,
    "GetEndpointsRequest": 428, "GetEndpointsResponse": 431,
    "OpenSecureChannelRequest": 446, "OpenSecureChannelResponse": 449,
    "CloseSecureChannelRequest": 452,
    "CreateSessionRequest": 461, "CreateSessionResponse": 464,
    "ActivateSessionRequest": 467, "ActivateSessionResponse": 470,
    "CloseSessionRequest": 473, "CloseSessionResponse": 476,
    "ReadRequest": 631, "ReadResponse": 634,
    "CreateMonitoredItemsRequest": 751, "CreateMonitoredItemsResponse": 754,
    "CreateSubscriptionRequest": 787, "CreateSubscriptionResponse": 790,
    "DataChangeNotification": 811, "StatusChangeNotification": 820,
    "PublishRequest": 826, "PublishResponse": 829,
    "DeleteSubscriptionsRequest": 847, "DeleteSubscriptionsResponse": 850,
}
STRUCT_BY_ID = {v: k for k, v in ENCODING_IDS.items()}


def _put_field(w, kind, value):
    if kind.endswith("[]"):
        if value is None:
            w.scalar("Int32", -1)
            return
        w.scalar("Int32", len(value))
        for item in value:
            _put_field(w, kind[:-2], item)
    elif kind in SCALARS:
        w.scalar(kind, value)
    elif kind in BUILTIN:
        BUILTIN[kind][0](w, value)
    else:
        w.raw(encode_struct(kind, value))


def _get_field(r, kind):
    if kind.endswith("[]"):
        n = r.scalar("Int32")
        if n == -1:
            return []
        if not 0 <= n <= MAX_ARRAY:
            raise DecodeError("array length")
        return [_get_field(r, kind[:-2]) for _ in range(n)]
    if kind in SCALARS:
        return r.scalar(kind)
    if kind in BUILTIN:
        return BUILTIN[kind][1](r)
    return decode_struct(kind, r)


def default(kind):
    if kind.endswith("[]"):
        return []
    if kind in SCALARS:
        return False if kind == "Boolean" else (0.0 if kind in ("Float", "Double") else 0)
    if kind == "NodeId":
        return NodeId()
    if kind in ("String", "ByteString", "LocalizedText", "DiagnosticInfo", "ExtensionObject", "Variant"):
        return None
    if kind == "QualifiedName":
        return (0, None)
    if kind == "DataValue":
        return DataValue()
    return make(kind)


def make(name, **fields):
    """A structure as a dict with defaults for omitted fields."""
    out = {}
    for field, kind in STRUCTS[name]:
        out[field] = fields.pop(field) if field in fields else default(kind)
    if fields:
        raise KeyError(f"unknown fields for {name}: {sorted(fields)}")
    return out


def encode_struct(name, value):
    w = Writer()
    for field, kind in STRUCTS[name]:
        _put_field(w, kind, value[field])
    return w.bytes()


def decode_struct(name, r):
    return {field: _get_field(r, kind) for field, kind in STRUCTS[name]}


def encode_message(name, value):
    """Service body: encoding NodeId followed by the structure."""
    w = Writer()
    put_nodeid(w, NodeId(0, ENCODING_IDS[name]))
    w.raw(encode_struct(name, value))
    return w.bytes()


def decode_message(data):
    r = Reader(data)
    node = get_nodeid(r)
    name = STRUCT_BY_ID.get(node.identifier) if node.namespace == 0 else None
    if name is None:
        raise DecodeError(f"unsupported service {node}")
    value = decode_struct(name, r)
    if r.remaining():
        raise DecodeError("trailing message bytes")
    return name, value


# --- Status codes (OPC 10000-6 Annex A StatusCode.csv) ---------------------------
GOOD = 0
STATUS = {
    "BadUnexpectedError": 0x80010000, "BadInternalError": 0x80020000, "BadEncodingError": 0x80060000,
    "BadDecodingError": 0x80070000, "BadTimeout": 0x800A0000, "BadServiceUnsupported": 0x800B0000,
    "BadNothingToDo": 0x800F0000, "BadTooManyOperations": 0x80100000, "BadCertificateInvalid": 0x80120000,
    "BadSecurityChecksFailed": 0x80130000, "BadCertificateUntrusted": 0x801A0000,
    "BadUserAccessDenied": 0x801F0000, "BadIdentityTokenInvalid": 0x80200000,
    "BadIdentityTokenRejected": 0x80210000, "BadSecureChannelIdInvalid": 0x80220000,
    "BadNonceInvalid": 0x80240000, "BadSessionIdInvalid": 0x80250000, "BadSessionClosed": 0x80260000,
    "BadSessionNotActivated": 0x80270000, "BadSubscriptionIdInvalid": 0x80280000,
    "BadRequestHeaderInvalid": 0x802A0000, "BadWaitingForInitialData": 0x80320000,
    "BadNodeIdInvalid": 0x80330000, "BadNodeIdUnknown": 0x80340000, "BadAttributeIdInvalid": 0x80350000,
    "BadSecurityPolicyRejected": 0x80550000, "BadTooManySessions": 0x80560000,
    "BadUserSignatureInvalid": 0x80570000, "BadApplicationSignatureInvalid": 0x80580000,
    "BadTooManyPublishRequests": 0x80780000, "BadNoSubscription": 0x80790000,
    "BadTcpMessageTypeInvalid": 0x807E0000, "BadTcpSecureChannelUnknown": 0x807F0000,
    "BadTcpMessageTooLarge": 0x80800000, "BadTcpEndpointUrlInvalid": 0x80830000,
    "BadSecureChannelClosed": 0x80860000, "BadSequenceNumberInvalid": 0x80880000,
    "BadProtocolVersionUnsupported": 0x80BE0000, "BadTypeMismatch": 0x80740000,
    "BadCommunicationError": 0x80050000, "BadServerHalted": 0x800E0000,
    "BadSensorFailure": 0x808C0000, "BadOutOfService": 0x808D0000,
    "UncertainLastUsableValue": 0x40900000, "UncertainSensorNotAccurate": 0x40930000,
}
STATUS_NAMES = {v: k for k, v in STATUS.items()}
OVERFLOW_BITS = 0x0480                         # InfoType DataValue (0x0400) + Overflow (0x0080)


def quality(status):
    """Severity bits 30-31: 00 Good, 01 Uncertain, 10/11 Bad (OPC 10000-4 7.39)."""
    return ("GOOD", "UNCERTAIN", "BAD", "BAD")[status >> 30 & 3]


def status_name(status):
    base = status & 0xFFFF0000
    name = STATUS_NAMES.get(base, "Good" if base == 0 else f"0x{base:08X}")
    return name + ("+Overflow" if status & OVERFLOW_BITS == OVERFLOW_BITS else "")
