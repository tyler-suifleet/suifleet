//! Minimal MQTT 3.1.1 client (protocol level 4) for the pub/sub bridge
//! (SF-032): CONNECT/CONNACK, PUBLISH at QoS 0 and 1, PUBACK, SUBSCRIBE/SUBACK,
//! PINGREQ/PINGRESP. MQTT 5 brokers accept 3.1.1 clients. The bridge keeps a
//! persistent session, so it never needs a clean DISCONNECT.
//! The local broker link is plain TCP to this device or a trusted local
//! network (TLS for LAN devices is SF-035); the AWS IoT Core link runs the
//! same session over mutual TLS (SF-061, `iotcore.rs`).
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
};

pub const MAX_PACKET: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Packet {
    ConnAck { session_present: bool, code: u8 },
    Publish { dup: bool, qos: u8, retain: bool, topic: String, packet_id: Option<u16>, payload: Vec<u8> },
    PubAck(u16),
    SubAck { packet_id: u16, codes: Vec<u8> },
    PingResp,
    Other(u8),
}

fn put_string(out: &mut Vec<u8>, text: &[u8]) {
    out.extend_from_slice(&(text.len() as u16).to_be_bytes());
    out.extend_from_slice(text);
}

pub(crate) fn packet(first: u8, body: Vec<u8>) -> Vec<u8> {
    let mut out = vec![first];
    let mut length = body.len();
    loop {
        let mut byte = (length % 128) as u8;
        length /= 128;
        if length > 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if length == 0 {
            break;
        }
    }
    out.extend(body);
    out
}

pub fn connect(client_id: &str, username: Option<&str>, password: Option<&[u8]>, keep_alive: u16, clean_session: bool) -> Vec<u8> {
    let mut body = Vec::new();
    put_string(&mut body, b"MQTT");
    body.push(4);
    let mut flags = if clean_session { 0x02 } else { 0 };
    if username.is_some() {
        flags |= 0x80;
    }
    if password.is_some() {
        flags |= 0x40;
    }
    body.push(flags);
    body.extend_from_slice(&keep_alive.to_be_bytes());
    put_string(&mut body, client_id.as_bytes());
    if let Some(user) = username {
        put_string(&mut body, user.as_bytes());
    }
    if let Some(pass) = password {
        put_string(&mut body, pass);
    }
    packet(0x10, body)
}

pub fn publish(topic: &str, payload: &[u8], qos: u8, packet_id: Option<u16>, dup: bool) -> Vec<u8> {
    let mut body = Vec::new();
    put_string(&mut body, topic.as_bytes());
    if qos > 0 {
        body.extend_from_slice(&packet_id.unwrap_or(1).to_be_bytes());
    }
    body.extend_from_slice(payload);
    packet(0x30 | ((dup as u8) << 3) | (qos << 1), body)
}

pub fn puback(packet_id: u16) -> Vec<u8> {
    packet(0x40, packet_id.to_be_bytes().to_vec())
}

pub fn subscribe(packet_id: u16, filters: &[(String, u8)]) -> Vec<u8> {
    let mut body = packet_id.to_be_bytes().to_vec();
    for (filter, qos) in filters {
        put_string(&mut body, filter.as_bytes());
        body.push(*qos);
    }
    packet(0x82, body)
}

pub fn pingreq() -> Vec<u8> {
    vec![0xc0, 0]
}

/// Decode one packet from the front of `buf`: the packet and its length, or
/// None when more bytes are needed.
pub fn decode(buf: &[u8]) -> anyhow::Result<Option<(Packet, usize)>> {
    if buf.len() < 2 {
        return Ok(None);
    }
    let (mut length, mut multiplier, mut index) = (0usize, 1usize, 1usize);
    loop {
        let Some(byte) = buf.get(index) else { return Ok(None) };
        length += (*byte as usize & 0x7f) * multiplier;
        index += 1;
        if byte & 0x80 == 0 {
            break;
        }
        multiplier *= 128;
        anyhow::ensure!(index <= 4, "malformed remaining length");
    }
    anyhow::ensure!(length <= MAX_PACKET, "packet too large");
    let total = index + length;
    if buf.len() < total {
        return Ok(None);
    }
    let body = &buf[index..total];
    let first = buf[0];
    let u16_at = |at: usize| -> anyhow::Result<u16> {
        body.get(at..at + 2).map(|b| u16::from_be_bytes([b[0], b[1]])).ok_or_else(|| anyhow::anyhow!("truncated packet"))
    };
    let decoded = match first >> 4 {
        2 => {
            anyhow::ensure!(body.len() == 2, "malformed CONNACK");
            Packet::ConnAck { session_present: body[0] & 1 == 1, code: body[1] }
        }
        3 => {
            let qos = (first >> 1) & 3;
            anyhow::ensure!(qos <= 1, "QoS 2 is not supported");
            let topic_length = u16_at(0)? as usize;
            let topic = std::str::from_utf8(body.get(2..2 + topic_length).ok_or_else(|| anyhow::anyhow!("truncated topic"))?)?.to_owned();
            let mut at = 2 + topic_length;
            let packet_id = if qos > 0 {
                let id = u16_at(at)?;
                at += 2;
                Some(id)
            } else {
                None
            };
            Packet::Publish { dup: first & 8 != 0, qos, retain: first & 1 != 0, topic, packet_id, payload: body[at..].to_vec() }
        }
        4 => Packet::PubAck(u16_at(0)?),
        9 => Packet::SubAck { packet_id: u16_at(0)?, codes: body[2..].to_vec() },
        13 => Packet::PingResp,
        other => Packet::Other(other),
    };
    Ok(Some((decoded, total)))
}

/// A byte stream a session runs over: TCP, or TLS over TCP.
pub trait Stream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Stream for T {}

/// The broker ended the connection cleanly (end of stream), as opposed to a
/// transport error or timeout. AWS IoT Core does this when a client
/// publishes to a topic its policy denies.
#[derive(Debug)]
pub struct Closed;

impl std::fmt::Display for Closed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("broker closed the connection")
    }
}
impl std::error::Error for Closed {}

/// A connected session: packets in, bytes out.
pub struct Session {
    stream: Box<dyn Stream>,
    buffer: Vec<u8>,
}

pub async fn connect_tcp(address: &str) -> anyhow::Result<TcpStream> {
    let stream = tokio::time::timeout(std::time::Duration::from_secs(10), TcpStream::connect(address)).await
        .map_err(|_| anyhow::anyhow!("connecting to {address} timed out"))??;
    stream.set_nodelay(true)?;
    Ok(stream)
}

impl Session {
    pub async fn open(address: &str, client_id: &str, username: Option<&str>, password: Option<&[u8]>, keep_alive: u16) -> anyhow::Result<(Self, bool)> {
        Self::start(Box::new(connect_tcp(address).await?), client_id, username, password, keep_alive).await
    }

    /// CONNECT over an established stream and wait for the CONNACK.
    pub async fn start(stream: Box<dyn Stream>, client_id: &str, username: Option<&str>, password: Option<&[u8]>, keep_alive: u16) -> anyhow::Result<(Self, bool)> {
        let mut session = Self { stream, buffer: Vec::new() };
        // A persistent session: the broker keeps our subscriptions and the QoS 1
        // messages that arrive while the bridge is away.
        session.send(&connect(client_id, username, password, keep_alive, false)).await?;
        let reply = tokio::time::timeout(std::time::Duration::from_secs(10), session.next()).await
            .map_err(|_| anyhow::anyhow!("no CONNACK within 10 s"))?;
        match reply? {
            Packet::ConnAck { session_present, code: 0 } => Ok((session, session_present)),
            Packet::ConnAck { code, .. } => anyhow::bail!("broker refused the connection (CONNACK {code})"),
            other => anyhow::bail!("expected CONNACK, got {other:?}"),
        }
    }

    pub async fn send(&mut self, bytes: &[u8]) -> anyhow::Result<()> {
        self.stream.write_all(bytes).await?;
        self.stream.flush().await?;
        Ok(())
    }

    pub async fn next(&mut self) -> anyhow::Result<Packet> {
        loop {
            if let Some((packet, used)) = decode(&self.buffer)? {
                self.buffer.drain(..used);
                return Ok(packet);
            }
            let mut chunk = [0u8; 16 * 1024];
            // TLS without close_notify reports an unexpected end of stream.
            let read = match self.stream.read(&mut chunk).await {
                Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => 0,
                other => other?,
            };
            if read == 0 {
                return Err(Closed.into());
            }
            self.buffer.extend_from_slice(&chunk[..read]);
            anyhow::ensure!(self.buffer.len() <= MAX_PACKET + 8, "packet too large");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_spec_vectors() {
        // MQTT 3.1.1 §3.1: CONNECT, client "c", clean session, keep alive 60.
        assert_eq!(connect("c", None, None, 60, true), [0x10, 13, 0, 4, b'M', b'Q', b'T', b'T', 4, 2, 0, 60, 0, 1, b'c']);
        assert_eq!(connect("c", Some("u"), Some(b"p"), 30, false)[9], 0xc0);
        assert_eq!(publish("a/b", b"hi", 1, Some(7), false), [0x32, 9, 0, 3, b'a', b'/', b'b', 0, 7, b'h', b'i']);
        assert_eq!(publish("a", b"", 0, None, false), [0x30, 3, 0, 1, b'a']);
        assert_eq!(publish("a", b"", 1, Some(1), true)[0], 0x3a);
        assert_eq!(puback(258), [0x40, 2, 1, 2]);
        assert_eq!(subscribe(5, &[("a/#".into(), 1)]), [0x82, 8, 0, 5, 0, 3, b'a', b'/', b'#', 1]);
        // Remaining length 321 is two bytes: 0xC1 0x02 (§2.2.3).
        let big = publish("t", &vec![0u8; 318], 0, None, false); // 2 + 1 + 318 = 321
        assert_eq!(&big[..3], &[0x30, 0xc1, 0x02]);
    }

    #[test]
    fn decodes_and_waits_for_whole_packets() {
        let bytes = publish("a/b", b"{\"v\":1}", 1, Some(9), true);
        assert_eq!(decode(&bytes[..bytes.len() - 1]).unwrap(), None);
        let (packet, used) = decode(&bytes).unwrap().unwrap();
        assert_eq!(used, bytes.len());
        assert_eq!(packet, Packet::Publish { dup: true, qos: 1, retain: false, topic: "a/b".into(), packet_id: Some(9), payload: b"{\"v\":1}".to_vec() });
        assert_eq!(decode(&[0x20, 2, 1, 0]).unwrap().unwrap().0, Packet::ConnAck { session_present: true, code: 0 });
        assert_eq!(decode(&[0x40, 2, 0, 9]).unwrap().unwrap().0, Packet::PubAck(9));
        assert_eq!(decode(&[0x90, 3, 0, 5, 1]).unwrap().unwrap().0, Packet::SubAck { packet_id: 5, codes: vec![1] });
        assert_eq!(decode(&[0xd0, 0]).unwrap().unwrap().0, Packet::PingResp);
        assert!(decode(&[0x34, 3, 0, 1, b'a']).is_err(), "QoS 2 refused");
        assert!(decode(&[0x30, 0xff, 0xff, 0xff, 0xff, 0x01]).is_err(), "malformed length");
    }
}
