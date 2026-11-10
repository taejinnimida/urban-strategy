"""Smoke the portable ZIP from a separate temporary filesystem directory."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import os
import subprocess
import sys
import zipfile

from standalone.build import build


class StandaloneBundleTest(TestCase):
    def test_bundle_contains_no_operational_seoul_code_and_runs_elsewhere(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            archive=build(root/"urban-survey-standalone.zip")
            target=root/"other-server"
            target.mkdir()
            with zipfile.ZipFile(archive) as zp:
                names=set(zp.namelist())
                self.assertNotIn("app.py",names)
                self.assertNotIn("app.html",names)
                self.assertNotIn("market_renewal.py",names)
                self.assertNotIn("nonurban.js",names)
                self.assertTrue({"dev_app.py","survey_sources.py","facts/vworld.py",
                                 "development/review.py","requirements.txt","Dockerfile"}
                                .issubset(names))
                zp.extractall(target)
            script=(
                "import sys;"
                "from fastapi.testclient import TestClient;"
                "from dev_app import app;"
                "assert 'app' not in sys.modules;"
                "c=TestClient(app);"
                "assert c.get('/').status_code==200;"
                "assert c.get('/survey-ui.js').status_code==200;"
                "assert c.get('/seoul').status_code==404;"
                "assert c.get('/nonurban').status_code==404;"
                "r=c.post('/api/development/snapshots',json={'geometry':"
                "{'type':'Polygon','coordinates':[[[127,37.5],[127.001,37.5],"
                "[127.001,37.501],[127,37.501],[127,37.5]]]}});"
                "assert r.status_code==200, (r.status_code,r.text);"
                "print('PASS independent survey from portable ZIP')"
            )
            result=subprocess.run([sys.executable,"-c",script],cwd=target,
                env={**os.environ,"VWORLD_API_KEY":""},
                capture_output=True,text=True,timeout=35)
            self.assertEqual(result.returncode,0,msg=result.stdout+"\n"+result.stderr)
