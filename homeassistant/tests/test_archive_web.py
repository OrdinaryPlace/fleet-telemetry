import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io

spec=importlib.util.spec_from_file_location('archive_web',Path(__file__).resolve().parents[1]/'addon'/'archive_web.py')
web=importlib.util.module_from_spec(spec);spec.loader.exec_module(web)

class ArchiveTests(unittest.TestCase):
    def test_download_preserves_zero_coordinates_and_resends_without_vin(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'2026-01-01_test_car.ndjson'
            row={'vehicle':'test_car','source_time':'2026-01-01T00:00:00.123456789Z','received_at':'2026-01-01T01:00:00Z','is_resend':True,'location':{'location_value':{'latitude':0,'longitude':0}}}
            path.write_text(json.dumps(row)+'\n'+json.dumps({**row,'location':{'invalid':True}})+'\n{"partial":')
            output=b''.join(web.csv_chunks(path)).decode()
            self.assertIn('0,0,True,True',output)
            self.assertIn(',,,True,False',output)
            self.assertEqual(len(output.splitlines()),3)
            self.assertIn('CSV',web.page(Path(folder)).decode())
            self.assertNotIn('latitude',web.page(Path(folder)).decode())
    def test_files_exclude_secrets_symlinks_and_arbitrary_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'options.json').write_text('secret')
            (root/'2026-01-01_test_car.ndjson').symlink_to(root/'options.json')
            self.assertEqual(web.archive_files(root),[])
            self.assertNotIn('secret',web.page(root).decode())
    def handler(self,peer,path):
        handler=object.__new__(web.Handler)
        handler.client_address=(peer,1);handler.path=path;handler.wfile=io.BytesIO()
        status=[];handler.send_headers=lambda *args,**kwargs:status.append((args,kwargs))
        return handler,status
    def test_ingress_peer_required_even_with_forged_header(self):
        handler,status=self.handler('127.0.0.1','/')
        handler.headers={'X-Forwarded-For':'172.30.32.2'}
        handler.do_GET()
        self.assertEqual(status[0][0][0],403)
    def test_download_rejects_path_traversal_and_serves_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(web,'ROOT',root):
                for query in ('../../options.json','%2Fdata%2Foptions.json','bad.ndjson'):
                    handler,status=self.handler('172.30.32.2','/download?file='+query+'&format=csv')
                    handler.do_GET();self.assertEqual(status[0][0][0],404)
                (root/'2026-01-01_test_car.ndjson').write_text('')
                handler,status=self.handler('172.30.32.2','/download?file=2026-01-01_test_car.ndjson&format=csv')
                handler.do_GET();self.assertEqual(status[0][0][0],200)
                self.assertIn(b'source_time_utc',handler.wfile.getvalue())

if __name__=='__main__': unittest.main()
