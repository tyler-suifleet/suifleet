#!/usr/bin/env python3
"""Offline Sui MovePackage::size guard (release bytecode v6/v7).

Matches MystenLabs/sui testnet-v1.83.0 crates/sui-types/src/move_package.rs:
version + module names/bytes + datatype origins + linkage entries. This is
package size, not merely the sum of module bytes. Unknown encodings fail closed.
"""
import argparse
import base64

LIMIT = 102_400


class Reader:
    def __init__(self, data):
        self.data, self.position = data, 0

    def take(self, count):
        end = self.position + count
        if end > len(self.data):
            raise ValueError('truncated Move bytecode')
        value = self.data[self.position:end]
        self.position = end
        return value

    def uleb(self):
        value = 0
        for shift in range(0, 35, 7):
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('invalid Move bytecode index')


def module_identity(data):
    reader = Reader(data)
    if reader.take(4) != bytes.fromhex('a11ceb0b') or int.from_bytes(reader.take(4), 'little') not in (6, 0x05000007):
        raise ValueError('unsupported Move bytecode format')
    tables = {}
    for _ in range(reader.uleb()):
        kind, offset, size = reader.take(1)[0], reader.uleb(), reader.uleb()
        if kind in tables:
            raise ValueError('duplicate Move table')
        tables[kind] = (offset, size)
    start = reader.position
    end = start + max(offset + size for offset, size in tables.values())
    self_index = Reader(data[end:]).uleb()

    def table(kind):
        offset, size = tables[kind]
        view = Reader(data[start + offset:start + offset + size])
        if len(view.data) != size:
            raise ValueError('truncated Move table')
        return view

    identifiers = []
    view = table(7)
    while view.position < len(view.data):
        identifiers.append(view.take(view.uleb()))
    modules = []
    view = table(1)
    while view.position < len(view.data):
        modules.append((view.uleb(), view.uleb()))
    name = identifiers[modules[self_index][1]]
    types = []
    view = table(2)
    while view.position < len(view.data):
        module, identifier = view.uleb(), view.uleb()
        view.uleb()  # ability set
        for _ in range(view.uleb()):
            view.uleb()  # type parameter constraints
            view.uleb()  # phantom flag
        if module == self_index:
            types.append(identifiers[identifier])
    return name, types


def check_package_size(built, limit=LIMIT):
    size = 8 + 72 * len(set(built['dependencies']))
    names = set()
    for encoded in built['modules']:
        data = base64.b64decode(encoded, validate=True)
        name, types = module_identity(data)
        if name in names:
            raise ValueError('duplicate package module')
        names.add(name)
        size += len(name) + len(data)
        size += sum(len(name) + len(datatype) + 32 for datatype in types)
    if not names:
        raise ValueError('empty Move package')
    if size > limit:
        raise ValueError(f'Move package size {size} exceeds {limit} bytes (before signing)')
    return size


def main():
    from sui_toolchain import compile_package
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', default='contracts')
    args = parser.parse_args()
    size = check_package_size(compile_package('sui', args.path))
    print(f'PASS: Move package {size}/{LIMIT} bytes; {LIMIT - size} bytes headroom')


if __name__ == '__main__':
    main()
