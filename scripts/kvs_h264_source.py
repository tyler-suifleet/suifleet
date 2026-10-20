#!/usr/bin/env python3
"""Bounded Annex-B H.264/AUD capture pipe -> SuiFleet version-1 media records.

Operator owns capture/encoding. Require constant frame rate, no B frames,
AUD delimiters and an IDR at least every 10 seconds. SPS/PPS must match config.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import sys

from kvs_video import Config, cluster, integer


def nals(source, limit):
    buffer = bytearray()
    while True:
        block = source.read1(65536) if hasattr(source, 'read1') else source.read(65536)
        buffer.extend(block)
        matches = list(re.finditer(b'\x00\x00(?:\x00)?\x01', buffer))
        if matches and matches[0].start() != 0:
            raise ValueError('Annex-B delimiter required')
        for first, second in zip(matches, matches[1:]):
            yield bytes(buffer[first.end():second.start()])
        if matches:
            buffer = buffer[matches[-1].start():]
        if len(buffer) > limit:
            raise ValueError('NAL limit exceeded')
        if not block:
            if matches:
                yield bytes(buffer[matches[-1].end():])
            elif buffer:
                raise ValueError('truncated Annex-B')
            return


def access_units(source, config):
    codec = config.codec
    size = int.from_bytes(codec[6:8], 'big')
    expected = {7: codec[8:8+size], 8: codec[11+size:]}
    unit, started = [], False
    total = 0
    for nal in nals(source, config.v['max_fragment_bytes']):
        if not nal:
            raise ValueError('empty NAL')
        kind = nal[0] & 31
        if kind == 9:
            if unit:
                yield unit
            unit, total, started = [], 0, True
        elif kind in (7, 8):
            if nal != expected[kind]:
                raise ValueError('capture codec changed; new spool required')
        elif kind in (1, 5, 6) and started:
            unit.append(nal)
            total += len(nal)+4
            if total > config.v['max_fragment_bytes']:
                raise ValueError('access unit limit exceeded')
        else:
            raise ValueError('unsupported Annex-B unit or missing AUD')
    if unit:
        yield unit


def records(source, config, start_ms, fps):
    fragment = []
    total = 0
    for index, unit in enumerate(access_units(source, config)):
        key = any(n[0] & 31 == 5 for n in unit)
        data = b''.join(struct.pack('>I', len(n))+n for n in unit)
        if key and fragment:
            cluster(fragment, config)  # reject before writing invalid media
            yield fragment
            fragment, total = [], 0
        fragment.append((start_ms+index*1000//fps, key, data))
        total += len(data)
        if (len(fragment) > 1000 or total > config.v['max_fragment_bytes']-256
                or fragment[-1][0]-fragment[0][0] > 10000):
            raise ValueError('capture GOP exceeds fragment limits')
    if fragment:
        cluster(fragment, config)
        yield fragment


def encode_record(frames):
    meta = json.dumps({'version': 1, 'frames': [{'timestamp_ms': t, 'keyframe': k, 'size': len(d)}
                       for t, k, d in frames]}, separators=(',', ':')).encode()
    if len(meta) > 65536:
        raise ValueError('media metadata limit exceeded')
    return struct.pack('>I', len(meta))+meta+b''.join(d for _, _, d in frames)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--start-ms', type=int, required=True)
    p.add_argument('--fps', type=int, required=True)
    args = p.parse_args()
    try:
        c = Config(json.loads(Path(args.config).read_text()))
        integer(args.start_ms, 1, 2**53-1)
        integer(args.fps, 1, 120)
        for frames in records(sys.stdin.buffer, c, args.start_ms, args.fps):
            sys.stdout.buffer.write(encode_record(frames))
            sys.stdout.buffer.flush()
    except (OSError, ValueError, KeyError, TypeError):
        print('video source rejected; check codec, AUD, timestamps and GOP limits', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
