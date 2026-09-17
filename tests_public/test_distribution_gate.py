"""Artifact-provenance preflight tests; no install or network in these fixtures."""
from __future__ import annotations
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/verify_distribution.py'
spec=importlib.util.spec_from_file_location('artifact_gate',SCRIPT)
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


class DistributionGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'source';self.root.mkdir()
        self.dist=Path(self.temp.name)/'dist';self.dist.mkdir()
        self.source={'src/memleaf/__init__.py':b'__version__="0.2.65"\n',
                     'src/memleaf/hermes_provider/plugin.yaml':b'name: memleaf\n',
                     'tests_public/test_one.py':b'import unittest\n',
                     'examples/incremental_acceptance.json':b'{"fixture":true}',
                     'pyproject.toml':b'[project]\nversion="0.2.65"\n',
                     'scripts/verify_distribution.py':b'# verifier\n'}
        for name,raw in self.source.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
        self.wheel=self.dist/'memleaf-0.2.65-py3-none-any.whl'
        self.sdist=self.dist/'memleaf-0.2.65.tar.gz'
        self.artifacts()

    def artifacts(self,*,wheel_override=None,sdist_override=None):
        wheel={name[len('src/'):]:raw for name,raw in self.source.items() if name.startswith('src/')}
        if wheel_override is not None:wheel=wheel_override
        with zipfile.ZipFile(self.wheel,'w') as z:
            for name,raw in wheel.items():z.writestr(name,raw)
        source=self.source if sdist_override is None else sdist_override
        with tarfile.open(self.sdist,'w:gz') as t:
            for name,raw in source.items():
                info=tarfile.TarInfo('memleaf-0.2.65/'+name);info.size=len(raw);t.addfile(info,io.BytesIO(raw))

    def test_prepare_binds_tests_resources_and_source(self):
        result=gate.prepare(self.root,self.dist,'a'*40)
        self.assertEqual(result['source_identity'],{k:gate.digest(v) for k,v in self.source.items()})
        self.assertEqual(result['commit'],'a'*40)
        self.assertEqual(result['package_version'],'0.2.65')

    def test_missing_tests_cannot_pass_as_zero(self):
        (self.root/'tests_public/test_one.py').unlink()
        with self.assertRaisesRegex(ValueError,'no_public_tests'):gate.prepare(self.root,self.dist,'a'*40)

    def test_changed_wheel_body_is_rejected(self):
        self.artifacts(wheel_override={'memleaf/__init__.py':b'wrong'})
        with self.assertRaisesRegex(ValueError,'wheel_source_mismatch'):gate.prepare(self.root,self.dist,'a'*40)

    def test_missing_provider_resource_is_rejected(self):
        self.artifacts(wheel_override={'memleaf/__init__.py':self.source['src/memleaf/__init__.py']})
        with self.assertRaisesRegex(ValueError,'wheel_source_mismatch'):gate.prepare(self.root,self.dist,'a'*40)

    def test_undeclared_extra_wheel_implementation_is_rejected(self):
        self.artifacts(wheel_override={'memleaf/__init__.py':self.source['src/memleaf/__init__.py'],
            'memleaf/hermes_provider/plugin.yaml':self.source['src/memleaf/hermes_provider/plugin.yaml'],
            'memleaf/extra.py':b'extra'})
        with self.assertRaisesRegex(ValueError,'wheel_source_mismatch'):gate.prepare(self.root,self.dist,'a'*40)

    def test_missing_sdist_test_is_rejected(self):
        self.artifacts(sdist_override={k:v for k,v in self.source.items() if not k.startswith('tests_public/')})
        with self.assertRaisesRegex(ValueError,'sdist_source_or_tests_mismatch'):gate.prepare(self.root,self.dist,'a'*40)

    def test_changed_sdist_fixture_is_rejected(self):
        self.artifacts(sdist_override={**self.source,'examples/incremental_acceptance.json':b'wrong'})
        with self.assertRaisesRegex(ValueError,'sdist_source_or_tests_mismatch'):gate.prepare(self.root,self.dist,'a'*40)

    def test_extra_sdist_test_is_rejected(self):
        self.artifacts(sdist_override={**self.source,'tests_public/test_extra.py':b'extra'})
        with self.assertRaisesRegex(ValueError,'sdist_extra_implementation_or_test'):gate.prepare(self.root,self.dist,'a'*40)

    def test_second_manifest_does_not_replace_original(self):
        gate.prepare(self.root,self.dist,'a'*40);before=(self.dist/'verification-manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'manifest_already_exists'):gate.prepare(self.root,self.dist,'b'*40)
        self.assertEqual((self.dist/'verification-manifest.json').read_bytes(),before)

    def test_wrong_commit_fails_before_install(self):
        gate.prepare(self.root,self.dist,'a'*40)
        with patch.object(gate.venv.EnvBuilder,'create',side_effect=AssertionError('no install')):
            with self.assertRaisesRegex(ValueError,'artifact_provenance_mismatch'):
                gate.verify(self.root,self.dist,Path(self.temp.name)/'output','b'*40)
        self.assertFalse((Path(self.temp.name)/'output').exists())

    def test_changed_artifact_bytes_fail_before_install(self):
        gate.prepare(self.root,self.dist,'a'*40)
        self.wheel.write_bytes(self.wheel.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'artifact_provenance_mismatch'):
            gate.verify(self.root,self.dist,Path(self.temp.name)/'output','a'*40)

    def test_changed_test_files_fail_before_install(self):
        gate.prepare(self.root,self.dist,'a'*40)
        (self.root/'tests_public/test_one.py').write_bytes(b'different')
        with self.assertRaisesRegex(ValueError,'sdist_source_or_tests_mismatch'):
            gate.verify(self.root,self.dist,Path(self.temp.name)/'output','a'*40)

    def test_multiple_distributions_rejected(self):
        (self.dist/'memleaf-other.whl').write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'exactly_one'):gate.prepare(self.root,self.dist,'a'*40)

    def test_archive_resource_bound_is_explicit(self):
        with patch.object(gate,'MAX_ARCHIVE_BYTES',1):
            with self.assertRaisesRegex(ValueError,'invalid_artifact_members'):gate.archive_files(self.wheel)


if __name__=='__main__':unittest.main()
