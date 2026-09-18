"""Verifier failures must not be reported as installed-package acceptance."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import run_contracts as runner
import verify_distribution as verify


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tar(self, members):
        path = self.root/'memleaf.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in members:
                item = tarfile.TarInfo(name); item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return path

    def wheel(self, members):
        path = self.root/'memleaf.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def payload(self):
        return {'__init__.py': b'__version__="test"',
                'hermes_provider/plugin.yaml': b'version: test',
                'hermes_provider/README.md': b'document',
                'hermes_provider/_provider.py': b'pass',
                'hermes_provider/evidence_budget.py': b'pass'}

    def test_safe_names_are_relative_and_portable(self):
        for name in ('../escape', '/abs', 'C:/abs', 'x\\y', 'a//b', 'a/./b', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):verify.safe_name(name)
        self.assertEqual(verify.safe_name('tests_public/中文.py'),'tests_public/中文.py')

    def test_tar_layout_strips_one_root(self):
        path=self.tar([('memleaf/src/memleaf/a.py', b'abc')])
        self.assertEqual(verify.archive_files(path), {'src/memleaf/a.py': b'abc'})

    def test_tar_rejects_multiple_roots(self):
        path=self.tar([('a/src/a',b'x'),('b/src/b',b'y')])
        with self.assertRaisesRegex(ValueError,'invalid_sdist_root'):verify.archive_files(path)

    def test_duplicate_member_is_not_last_wins(self):
        path=self.tar([('memleaf/a',b'first'),('memleaf/a',b'second')])
        with self.assertRaisesRegex(ValueError,'duplicate'):verify.archive_files(path)

    def test_tar_link_not_extracted(self):
        path=self.tar([('memleaf/a', b'original')])
        with tarfile.open(path,'w:gz') as archive:
            item=tarfile.TarInfo('memleaf/link');item.type=tarfile.SYMTYPE;item.linkname='/private'
            archive.addfile(item)
        with self.assertRaisesRegex(ValueError,'invalid_archive_member'):verify.archive_files(path)

    def test_oversize_file_fails_before_read(self):
        path=self.tar([('memleaf/a',b'12345')])
        with patch.object(verify,'MAX_FILE',3),self.assertRaises(ValueError):verify.archive_files(path)

    def test_total_bound_not_only_individual_bound(self):
        path=self.wheel([('memleaf/a',b'1234'),('memleaf/b',b'5678')])
        with patch.object(verify,'MAX_BYTES',7),self.assertRaisesRegex(ValueError,'archive_too_large'):verify.archive_files(path)

    def test_exact_package_includes_provider_data(self):
        data=self.payload()
        a={'memleaf/'+k:v for k,v in data.items()};b={'src/memleaf/'+k:v for k,v in data.items()}
        self.assertEqual(len(verify.assert_same_package(a,b)),5)
        b['src/memleaf/hermes_provider/plugin.yaml']=b'changed'
        with self.assertRaisesRegex(ValueError,'payload_mismatch'):verify.assert_same_package(a,b)

    def test_missing_resource_is_not_source_match_success(self):
        data=self.payload();del data['hermes_provider/plugin.yaml']
        with self.assertRaisesRegex(ValueError,'provider_resource_missing'):
            verify.assert_same_package({'memleaf/'+k:v for k,v in data.items()}, {'src/memleaf/'+k:v for k,v in data.items()})

    def test_empty_package_rejected(self):
        with self.assertRaisesRegex(ValueError,'package_missing'):verify.package_files({})

    def test_test_workspace_excludes_source(self):
        files={'tests_public/test_a.py':b'pass','examples/input.json':b'{}','src/memleaf/a.py':b'pass'}
        selected=verify.suite_assets(files)
        self.assertEqual(set(selected),{'tests_public/test_a.py','examples/input.json'})
        verify.write_files(self.root/'fixture',selected)
        self.assertFalse((self.root/'fixture/src').exists())

    def test_existing_workspace_is_not_overwritten(self):
        out=self.root/'workspace';out.mkdir()
        with self.assertRaises(FileExistsError):verify.write_files(out,{'a':b'x'})

    def test_zero_or_different_test_inventory_has_distinct_fingerprint(self):
        self.assertNotEqual(runner.inventory_digest([]),runner.inventory_digest(['test_a']))
        self.assertNotEqual(runner.inventory_digest(['test_a']),runner.inventory_digest(['test_b']))

    def test_discovery_tracks_duplicates_and_order_is_canonical(self):
        class Demo(unittest.TestCase):
            def test_one(self):pass
        suite=unittest.TestSuite([Demo('test_one'),unittest.TestSuite([Demo('test_one')])])
        ids=runner.test_ids(suite)
        self.assertEqual(len(ids),2);self.assertEqual(ids[0],ids[1])

    def test_import_root_must_be_the_selected_installation(self):
        expected=self.root/'env/site-packages/memleaf'
        self.assertTrue(runner.verified_package(str(expected/'__init__.py'),expected))
        self.assertFalse(runner.verified_package(str(self.root/'src/memleaf/__init__.py'),expected))

    def test_clean_environment_does_not_forward_live_configuration(self):
        with patch.dict('os.environ', {'PYTHONPATH':'other-source','MEMLEAF_VAULT':'private','OPENAI_API_KEY':'secret'}):
            env=verify.clean_env(self.root)
        self.assertNotIn('PYTHONPATH',env);self.assertNotIn('MEMLEAF_VAULT',env);self.assertNotIn('OPENAI_API_KEY',env)
        self.assertEqual(env['HOME'],str(self.root))

    def test_manifest_requires_successful_source_tests(self):
        payload=self.payload()
        self.wheel([('memleaf/'+n,v) for n,v in payload.items()])
        self.tar([('memleaf/src/memleaf/'+n,v) for n,v in payload.items()]+[('memleaf/tests_public/test_a.py',b'pass')])
        report=self.root/'source.json';report.write_text(json.dumps({'status':'failed','tests':1,'skipped':[]}))
        with self.assertRaisesRegex(ValueError,'source_tests_not_passed'):verify.manifest(self.root,report,'a'*40)

    def test_manifest_binds_commit_and_exact_artifact_bytes(self):
        payload=self.payload()
        w=self.wheel([('memleaf/'+n,v) for n,v in payload.items()])
        self.tar([('memleaf/src/memleaf/'+n,v) for n,v in payload.items()]+[('memleaf/tests_public/test_a.py',b'pass')])
        report=self.root/'source.json';report.write_text(json.dumps({'status':'passed','tests':1,'skipped':[]}))
        value=verify.manifest(self.root,report,'a'*40)
        raw=json.dumps(value).encode();(self.root/'verification-manifest.json').write_bytes(raw)
        verify.verify_binding(self.root,verify.digest(raw),'a'*40)
        with self.assertRaisesRegex(ValueError,'commit_mismatch'):verify.verify_binding(self.root,verify.digest(raw),'b'*40)
        with self.assertRaisesRegex(ValueError,'manifest_mismatch'):verify.verify_binding(self.root,'0'*64,'a'*40)
        w.write_bytes(w.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'bytes_mismatch'):verify.verify_binding(self.root,verify.digest(raw),'a'*40)

    def test_skipped_source_tests_do_not_create_a_passing_manifest(self):
        payload=self.payload();self.wheel([('memleaf/'+n,v) for n,v in payload.items()])
        self.tar([('memleaf/src/memleaf/'+n,v) for n,v in payload.items()])
        report=self.root/'source.json';report.write_text(json.dumps({'status':'passed','tests':1,'skipped':['some']}))
        with self.assertRaisesRegex(ValueError,'source_tests_not_passed'):verify.manifest(self.root,report,'a'*40)
