"""Raw-byte SDK chunking and partial backpressure retry, no daemon/network."""
import base64,hashlib,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'sdk/python'))
from suifleet_ipc import Client,DataPutError,IpcError
class OpaqueSdk(unittest.TestCase):
    def test_partial_retry_preserves_full_file_geometry_and_bytes(self):
        data=bytes(range(256))*200;calls=[]
        class Fake(Client):
            def request(self,operation,**fields):
                descriptor=fields['payload']['descriptor'];chunk=descriptor['chunk'];calls.append(fields)
                if chunk==1:raise IpcError('full')
                return {'sequence':chunk,'state':'queued','storage_acknowledged':False}
        with self.assertRaises(DataPutError) as error:Fake().put_data('sensor','raw',data,upload='ab'*32)
        self.assertEqual((error.exception.upload,error.exception.chunk),('ab'*32,1))
        calls.clear()
        class Retry(Client):
            def request(self,operation,**fields):calls.append(fields);return {'storage_acknowledged':False}
        Retry().put_data('sensor','raw',data,upload=error.exception.upload,start_chunk=error.exception.chunk)
        self.assertEqual([v['payload']['descriptor']['chunk'] for v in calls],[1,2])
        for v in calls:
            m=v['payload']['descriptor'];chunk=base64.b64decode(v['payload']['data_base64'])
            self.assertEqual(chunk,data[m['chunk']*24576:(m['chunk']+1)*24576]);self.assertEqual(m['sha256'],list(hashlib.sha256(chunk).digest()))
            self.assertEqual((m['chunks'],m['total_size']),(3,len(data)))
if __name__=='__main__':unittest.main()
