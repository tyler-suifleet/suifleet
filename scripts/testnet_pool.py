#!/usr/bin/env python3
"""Public, bounded CI lane identities. Signing keys never belong in this file."""
import argparse
import os

PRIMARY = '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac'
ADDRESSES = (PRIMARY,
             '0x0afccf110391cafb2844a986d20d6633436e2586c4e05e7c08fbc510dbfdcaec',
             '0xea997324bbc7762fae14aeaa4111e448d1617e935d997eae37b8cc3a7ae0c6b4')
AGENT = '0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'
TESTNET_WALLETS = {AGENT, *ADDRESSES}


def lane_info(lane):
    if type(lane) is not int or not 0 <= lane < len(ADDRESSES):
        raise ValueError('invalid dedicated CI wallet lane')
    return dict(lane=str(lane), address=ADDRESSES[lane],
                secret='SUIFLEET_TESTNET_KEY' + (f'_{lane}' if lane else ''),
                fixture='encrypted-testnet-fixture' + (f'-lane-{lane}' if lane else ''),
                # Primary retains the old lock while old controllers drain.
                concurrency='suifleet-testnet-ci-wallet' + (f'-lane-{lane}' if lane else ''))


def select(run_id, size='1'):
    if str(size) not in ('1', '2', '3') or not str(run_id).isdigit() or int(run_id) <= 0:
        raise ValueError('pool size must be 1..3 and run identity positive')
    # No matrix fanout: exactly one lane per workflow, stable across reruns.
    return lane_info(int(run_id) % int(size))


def configured_address():
    lane = os.environ.get('SUIFLEET_TESTNET_LANE', '0')
    if lane not in ('0', '1', '2'):
        raise ValueError('invalid dedicated CI lane')
    expected = lane_info(int(lane))['address']
    if os.environ.get('SUIFLEET_TESTNET_ADDRESS', expected) != expected:
        raise ValueError('dedicated CI lane/address mismatch')
    return expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--select', action='store_true')
    args = parser.parse_args()
    result = select(os.environ['GITHUB_RUN_ID'], os.environ.get('SUIFLEET_TESTNET_POOL_SIZE') or '1')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in result.items():
            print(f'{key}={value}', file=stream)
