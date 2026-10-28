import copy
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import testnet_pool as pool
import testnet_prepared as prepared
import testnet_fixture_cache as cache
import ci_testnet_wallet as wallet


class PoolTests(unittest.TestCase):
    def test_exactly_one_balanced_lane_and_primary_compatible_names(self):
        selected=[pool.select(i,'3') for i in range(1,31)]
        self.assertEqual([sum(x['lane']==str(i) for x in selected) for i in range(3)],[10,10,10])
        self.assertEqual(pool.select(99)['concurrency'],'suifleet-testnet-ci-wallet')
        self.assertEqual(pool.select(99)['fixture'],cache.NAME)
        self.assertEqual(pool.select(99)['secret'],'SUIFLEET_TESTNET_KEY')
        for size in ('0','4','-1','3\n','abc'):
            with self.assertRaises(ValueError):pool.select(3,size)
        for run in ('','0','-1','a'):
            with self.assertRaises(ValueError):pool.select(run,3)

    def test_lanes_have_distinct_keys_addresses_histories_and_locks(self):
        lanes=[pool.lane_info(i) for i in range(3)]
        for field in ('secret','address','fixture','concurrency'):
            self.assertEqual(len({x[field] for x in lanes}),3)
        # Changing pool size/rerunning never changes a wallet's lock/history.
        for size in (1,2,3):
            for run in range(1,20):
                selected=pool.select(run,size)
                self.assertEqual(selected,pool.lane_info(int(selected['lane'])))
        with self.assertRaises(ValueError):pool.lane_info(True)

    def test_lane_address_mismatch_and_unregistered_addresses_are_refused(self):
        for lane in range(3):
            with patch.dict(os.environ,{'SUIFLEET_TESTNET_LANE':str(lane),'SUIFLEET_TESTNET_ADDRESS':pool.ADDRESSES[lane]}):
                self.assertEqual(pool.configured_address(),pool.ADDRESSES[lane])
            with patch.dict(os.environ,{'SUIFLEET_TESTNET_LANE':str(lane),'SUIFLEET_TESTNET_ADDRESS':pool.AGENT}):
                with self.assertRaises(ValueError):pool.configured_address()
        js=(Path(__file__).resolve().parents[1]/'ci_testnet.cjs').read_text()
        self.assertTrue(all(address in js for address in pool.ADDRESSES))

    def test_cache_queries_downloads_and_prunes_only_the_selected_lane(self):
        for lane in range(3):
            name=pool.lane_info(lane)['fixture'];calls=[]
            def get(url):
                calls.append(url)
                if '/artifacts?' in url:
                    self.assertIn('name='+name+'&',url)
                    return {'total_count':2,'artifacts':[
                        {'id':10,'name':name,'expired':False,'created_at':'2026-10-28T00:00:00Z','workflow_run':{'id':7}},
                        {'id':11,'name':'wrong-lane','expired':False,'created_at':'2026-10-29T00:00:00Z','workflow_run':{'id':8}}]}
                return {'id':7,'head_branch':'main','path':cache.WORKFLOW,'repository':{'full_name':'a/b'},'event':'workflow_run'}
            def download(run,artifact,destination):
                self.assertEqual((run,artifact),(7,name));(destination/'fixture.bin').write_bytes(b'authenticated-ciphertext')
            with tempfile.TemporaryDirectory() as d:
                self.assertTrue(cache.restore('a/b',d,get,download,name=name)['restored'])
            deleted=[]
            cache.prune('a/b',12,get,deleted.append,name=name)
            self.assertEqual(deleted,[])

    def test_selected_wallet_config_and_allowances_are_preserved(self):
        for lane in range(3):
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'SUIFLEET_TESTNET_LANE':str(lane),'SUIFLEET_TESTNET_ADDRESS':pool.ADDRESSES[lane]}):
                def run(command,**kwargs):
                    self.assertIn(pool.ADDRESSES[lane],(Path(d)/'client.yaml').read_text())
                    self.assertIn(str(wallet.PACKAGE_UPGRADE_CEILING),command)
                    self.assertIn('--required-balance-mist',command)
                    return type('Result',(),{'returncode':0})()
                self.assertEqual(wallet.execute(d,str(Path(d)/'state.json'),'a'*40,b'[]',run,relevant=lambda _:False),0)


if __name__=='__main__':unittest.main()
