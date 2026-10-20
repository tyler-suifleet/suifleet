# Kinesis Video media ingestion (SF-072)

The Linux component adapter in [`scripts/kvs_video.py`](../scripts/kvs_video.py)
accepts encoded H.264 media, creates Matroska fragments, durably buffers them and
uploads multiple fragments per signed, duplex Kinesis Video `PutMedia` session.
It is separate from SF-034's generic HTTP stream export: media needs its own binary
store, codec headers, timestamps and fragment acknowledgements. It uses the SF-057
credential-provider contract; no daemon, Move, Sui manifest or shared IPC API changes
are required. No media, camera secrets or AWS credentials are put on chain.

The versioned [media/configuration contract](../interfaces/compat/video/contract.json)
pins the reviewed public references: EdgeConnectorForKVS 1.0.5, Kinesis Video API
2017-09-30, Matroska RFC 9559 (October 2024), PIC v1.3.0 ACK field declarations and
C producer v1.5.0 streaming signing semantics. HTTP reference snapshot hashes and
immutable upstream commit links are recorded there. No upstream implementation or
closed-source connector code is incorporated. FFmpeg is a separate test/capture
process, not linked or bundled into this component. The generated
[test pattern](../interfaces/compat/video/fixture.json) is CC0-1.0.

This is a native functional media subset, not an AWS recipe/configuration or
TwinMaker/SiteWise/Secrets Manager replacement. The component takes an explicitly
configured capture stream and an existing destination stream. It does not discover
cameras, create AWS resources or reproduce the proprietary connector's control
plane. RTSP authentication, camera transport, encoding and clock synchronization
belong to the operator's capture process. Audio, B frames, codec changes within a
spool and zero-retention streams are unsupported. Persisted ACKs require a stream
with storage retention enabled. Live AWS interoperability remains unrun.

## Configure and run

Install the reviewed `kvs_video.py` and `kvs_h264_source.py` beside the existing
SF-057 `aws_credentials.py` module under a root-owned code directory readable by
the component UID. The video client imports its kernel TCP-UID lookup helper; it
does not start a broker under the component identity. Use an ordinary configured
component lifecycle or platform service to supervise two processes: capture/ingest
and upload. Provision a distinct component UID, an owner-only persistent state
directory and an operator-controlled JSON configuration. Preserve that directory
across component and device restarts; never share it between cameras. Runtime
requires Linux, Python 3.10+ with SQLite and SF-057 credentials. It has no pip
runtime dependencies. This ticket does not build a Yocto image/package.

Start with [`example.json`](../interfaces/compat/video/example.json). The example's
codec private bytes and 32x32 dimensions match only the committed deterministic
source. For a camera, configure its dimensions and AVCDecoderConfigurationRecord
from the encoder's SPS/PPS. Version 1 requires H.264 baseline, four-byte AVCC NAL
lengths, one SPS and one PPS. Stop/reconfigure to a new state directory when the
codec or destination changes; existing buffered media must be drained or retained
for inspection, never relabeled for another stream. Quota/session limits may change
without relabeling media, but lowering quotas below retained use is refused.

Use the [SF-057 provisioning instructions](AWS_CREDENTIALS.md) to grant this component
an AWS container-credential endpoint/token. Set the required
`credential_provider_uid` to the trusted broker socket-owner UID (the example
uses 0 for a root broker), not the camera component UID. Before sending its local
authorization token, the client matches the exact peer TCP tuple in Linux
`/proc/net/tcp` against that configured UID. Missing, ambiguous or wrong identity
fails closed. This prevents another Unix UID taking over a stopped broker
port from receiving the token or substituting role credentials. The client needs
a visible `/proc/net/tcp` in the same network namespace, as SF-057 already requires.
Changing this trusted UID changes the spool binding; drain/archive old media
before provisioning a new binding. No automatic credential-source detection occurs.
Its role needs only
`kinesisvideo:GetDataEndpoint` and `kinesisvideo:PutMedia` on the selected existing
stream. The uploader accepts only the SF-057 literal-loopback credential URI; it
has no environment/static-key/profile fallback. It resolves fresh credentials and
a signed `GetDataEndpoint` before each session. Production control/data requests
use regional AWS HTTPS endpoints with CA/hostname validation, no redirects or
proxy environment. `emulator: true` explicitly selects literal-loopback HTTP
endpoints and is only for disposable local tests. No credentials are written to
the spool or printed in diagnostics.

As the configured component UID, wrap the uploader with SF-057's exec wrapper:

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/provider.sock --component camera -- \
  python3 /usr/libexec/suifleet/kvs_video.py \
  --config /etc/suifleet-video/camera.json upload
```

Use the actual wrapper socket/component name from your provisioned policy. The
wrapper's task must stay alive; do not background its child. See
[AWS_CREDENTIALS.md](AWS_CREDENTIALS.md) for UID and token-lifetime constraints.
`upload-once` runs one bounded session and returns nonzero if the offered media
was not all persisted. `upload` reconnects with exponential delays of 1–60 seconds;
when there is no media it polls once a second. Each session is limited to 1–300
seconds, at most 1000 queued fragments and five seconds before credential expiry.
The sender paces at no more than five fragments/second and 12 MB/second. Per-socket
I/O has a five-second idle timeout and sessions have a total deadline.

A capture encoder can pipe Annex-B H.264 with AUD delimiters, constant frame rate,
no B frames and an IDR at least every ten seconds to the source adapter, then ingest.
For example, this *software* source matches the example configuration:

```sh
set -o pipefail
ffmpeg -f lavfi -i 'testsrc2=size=32x32:rate=5' -t 2 \
  -an -c:v libx264 -profile:v baseline -pix_fmt yuv420p \
  -x264-params 'keyint=5:min-keyint=5:scenecut=0:aud=1' -f h264 - |
python3 /usr/libexec/suifleet/kvs_h264_source.py \
  --config /etc/suifleet-video/camera.json --fps 5 --start-ms 1700000000000 |
python3 /usr/libexec/suifleet/kvs_video.py \
  --config /etc/suifleet-video/camera.json ingest
```

For live capture, supply the actual absolute timestamp of the first captured frame,
not the example timestamp. The configured FPS derives subsequent timestamps;
variable-frame-rate sources should use the binary contract directly with actual
capture timestamps. The source verifies AUD boundaries and configured SPS/PPS,
groups access units into IDR-first fragments, and rejects unsupported NALs or an
oversized GOP. It cannot infer camera clock drift or compensate for upstream
frame loss. A pipe may contain a partially built fragment when killed: only
fragments with a `queued`/`duplicate` ingest receipt were durably admitted. For
lossless capture, the source must retain/retry unreceipted fragments. The supplied
pipe does not persist incomplete GOPs or guarantee lossless camera acquisition.

## Persistence and failure handling

Every queued fragment, its SHA-256 and the admission high-water timestamps commit
in one SQLite `synchronous=FULL` transaction before the ingest receipt. Fragment
and frame times increase strictly; fragments must start at least 200 ms apart and
never overlap. Exact pending-fragment retries and the latest admission retry are
idempotent, including after retirement. Older replay is rejected; no unbounded
historical deduplication table exists. Input framing/version, quotas and media
limits are checked before admission. Filesystem durability assumes working
`fsync` semantics and persistent storage; this is not physical power-loss evidence.

The configured quota charges media plus 256 bytes per fragment and also caps the
fragment count. Full storage rejects the API admission; `ingest` blocks source
reads while retaining one bounded fragment until the independent uploader releases
space. It never evicts unacknowledged video. SQLite page/schema allocation adds overhead that depends on page size and
fragment packing; rollback transactions can temporarily duplicate main-file pages.
The media quota is not a hard filesystem-byte ceiling. Reserve space for those
allocations and journals, and use platform filesystem quotas if a hard disk ceiling
is required. FULL auto-vacuum reclaims retired pages. The source, ingester and sender each hold bounded fragment
buffers; pending timestamps are limited to 1000 per session.

`BUFFERING`, `RECEIVED`, HTTP 200 and connection closure do not retire media.
Only a valid `PERSISTED` ACK for a fragment offered in this session does. Persisted
fragments are removed atomically, so a crash after that commit does not resend them.
An ambiguous response can replay a remotely persisted fragment with identical bytes
and absolute timestamps. This is **at-least-once**, not remote deduplication or
exactly-once delivery. Consumers must tolerate duplicates. Persisted ACKs identify individual fragments. Retirement waits for a contiguous
persisted prefix: a later persisted fragment stays buffered across an earlier
unacknowledged gap, including restart. This preserves the SF-064 delivery contract
while using actual media fragment timestamps rather than generic file offsets.

Disconnects, missing ACKs, 401/403 HTTP rejection, throttling, credential expiry,
invalid/oversized ACKs and retryable error IDs retain media. Error ACK IDs 4000,
4003, 5000 and 5001 reconnect; all other ERROR ACKs durably pause the spool and stop
the uploader. The sanitized pause marker survives process restarts. Inspect and
correct the stream/codec/permissions and preserve rejected media before deciding
whether to retry it. There is deliberately no automatic skip/reset/delete command.
An operator may stop both processes, back up the directory and clear the SQLite
`failure` row after resolving the cause; this permits replay of the same fragment.
Only one uploader can hold the per-spool advisory lock. Media hashes are verified
before transmission; corruption retains the affected fragment and fails the session.

## Reproducible evidence and remaining acceptance

```sh
make test-video
```

The test-only pinned Ubuntu/FFmpeg image is prepared with public package downloads;
the media suite then executes as a non-root UID with `--network none`, a read-only
checkout, bounded tmpfs, no capabilities and no host credentials. Two additional
credential-issuer tests use a second disposable container with only DAC_OVERRIDE,
SETUID and SETGID capabilities to load the checkout and drop to distinct broker
and client UIDs. They prove a correct peer UID succeeds and an incorrect one
receives no authorization token. The test image does not alter the daemon
image/toolchain. Tests run the real source, ingester and uploader subprocesses,
check SigV4 independently, parse MKV clusters/timestamps in the emulator, and decode
the captured multi-fragment MKV with unmodified FFmpeg against ten expected pixel
hashes. Failure cases cover credential expiry/rotation, partial persistence, lost
responses/replay, HTTP and ACK rejection, ACK framing, byte/count backpressure,
source truncation, corrupt media, single-uploader exclusion, session timeout and
SIGKILL/restart, plus the real-UID credential-issuer checks. The normal CI workflow includes this suite.

The [runtime receipt](validation/sf072-video.json) records exact commands/results.
The SF-064 roadmap models remain specification fixtures, not proof that this code
executed. Live AWS API/retention/signature/endpoint interoperability requires separate
user authorization. The compatibility limit is recorded in existing open
[SF-037/#36](https://github.com/tyler-suifleet/suifleet/issues/36#issuecomment-6279814529)
and in this implementation
[SF-072/#126](https://github.com/tyler-suifleet/suifleet/issues/126).
SF-064/#109 is a closed planning/history record. This records a future authorization
requirement, without adding a paid/live AWS gate to SF-037 implementation.
No AWS accounts/resources, paid services or production credentials were used.
Physical Pi/camera/network/reboot/power-loss integration remains
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32); no image or physical
result is claimed. Normal current-head CI and automatic Sui testnet checks remain
required by the PR service, but this adapter does not modify the Sui transaction path.
