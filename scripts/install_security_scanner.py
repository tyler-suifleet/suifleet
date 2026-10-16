#!/usr/bin/env python3
"""Install a pinned scanner without executing a mutable third-party action/script."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import urllib.request

VERSION = '0.76.0'
SHA256 = 'e24b1c51293c1b15e4ba8d45bbc7891ba053f90278ec2698f3562c993e145fe7'
URL = f'https://github.com/aquasecurity/trivy/releases/download/v{VERSION}/trivy_{VERSION}_Linux-64bit.tar.gz'


def install(output):
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError('security scanner archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        member = bundle.getmember('trivy')
        if not member.isfile():
            raise ValueError('invalid scanner archive entry')
        binary = bundle.extractfile(member).read()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_bytes(binary)
    output.chmod(0o700)
    print(f'Installed checksum-verified Trivy {VERSION}.')


if __name__ == '__main__':
    install(Path(sys.argv[1]))
