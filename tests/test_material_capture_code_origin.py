"""Offline provenance checks; source files are fixtures, never website evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.ai.material_capture import capture_materials


class MaterialCaptureCodeOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='watcher-code-origin-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        forbidden=AssertionError('Code provenance checks forbid network and databases')
        for target in ('socket.getaddrinfo','socket.socket.connect','sqlite3.connect'):
            guard=patch(target,side_effect=forbidden);guard.start();self.addCleanup(guard.stop)

    def load_override(self):
        path=self.root/'run_native_branch_pilot.py'
        path.write_text('VERSION = "repaired-fixture"\n',encoding='utf-8')
        name='scripts.checks.run_native_branch_pilot'
        spec=importlib.util.spec_from_file_location(name,path)
        loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded)
        return path,name,loaded

    def manifest(self):
        with capture_materials(self.root/'records',label='offline-code-origin') as session:
            pass
        return session,json.loads((session.path/'manifest.json').read_text(encoding='utf-8'))['payload']

    def test_loaded_override_is_hashed_instead_of_baseline_file(self):
        path,name,loaded=self.load_override()
        with patch.dict(sys.modules,{name:loaded}):session,payload=self.manifest()
        key='scripts/checks/run_native_branch_pilot.py'
        self.assertEqual(payload['code_sha256'][key],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(payload['code_origins'][key]['basis'],'loaded_module_file')
        self.assertEqual(Path(payload['code_origins'][key]['path']),path.resolve())
        self.assertTrue(session.complete)

    def test_unloaded_source_is_explicit_and_not_imported_for_capture(self):
        name='scripts.checks.run_native_branch_pilot'
        with patch.dict(sys.modules,{name:None}):
            session,payload=self.manifest()
            self.assertIsNone(sys.modules[name])
        origin=payload['code_origins']['scripts/checks/run_native_branch_pilot.py']
        self.assertEqual(origin['basis'],'source_file_not_loaded')
        self.assertFalse(origin['loaded'])
        self.assertTrue(session.complete)

    def test_missing_loaded_file_is_a_gap_without_baseline_substitution(self):
        path,name,loaded=self.load_override();path.unlink()
        with patch.dict(sys.modules,{name:loaded}):session,payload=self.manifest()
        key='scripts/checks/run_native_branch_pilot.py'
        self.assertNotIn(key,payload['code_sha256'])
        self.assertEqual(payload['code_origins'][key]['basis'],'loaded_module_file_unavailable')
        self.assertFalse(session.complete)
        self.assertIn('loaded_code_file_unavailable',session.gaps)


if __name__=='__main__':unittest.main()
