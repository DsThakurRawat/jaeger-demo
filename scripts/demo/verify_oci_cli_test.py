# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from importlib import metadata
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import verify_oci_cli


class OciCliVersionTests(unittest.TestCase):
    def setUp(self):
        self.pins = verify_oci_cli.read_constraints()

    def test_constraints_retain_the_exact_observed_versions(self):
        self.assertEqual('3.96.3', self.pins['oci-cli'])
        self.assertEqual('2.192.0', self.pins['oci'])
        self.assertEqual('2.9.0', self.pins['python-dateutil'])

    def test_rejects_missing_malformed_and_duplicate_pins(self):
        for content in ('', 'oci-cli>=3.96.3\n', 'oci-cli==3.96.3\noci_cli==3.96.3\n'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'constraints.txt'
                path.write_text(content)
                with self.assertRaises(ValueError):
                    verify_oci_cli.read_constraints(path)

    @patch('verify_oci_cli.subprocess.check_output')
    @patch('verify_oci_cli.metadata.version')
    def test_accepts_matching_distributions_and_cli(self, version, cli):
        version.side_effect = self.pins.__getitem__
        cli.return_value = self.pins['oci-cli'] + '\n'
        verify_oci_cli.verify_versions(self.pins)
        self.assertEqual(list(self.pins), [call.args[0] for call in version.call_args_list])
        cli.assert_called_once_with(['oci', '--version'], text=True)

    @patch('verify_oci_cli.subprocess.check_output')
    @patch('verify_oci_cli.metadata.version')
    def test_rejects_dependency_drift_even_when_cli_version_matches(self, version, cli):
        installed = dict(self.pins, oci='0.0.0')
        version.side_effect = installed.__getitem__
        with self.assertRaisesRegex(RuntimeError, 'oci: expected 2.192.0, got 0.0.0'):
            verify_oci_cli.verify_versions(self.pins)
        cli.assert_not_called()

    @patch('verify_oci_cli.metadata.version', side_effect=metadata.PackageNotFoundError('oci'))
    def test_rejects_missing_distribution(self, version):
        with self.assertRaisesRegex(RuntimeError, 'Missing OCI dependency'):
            verify_oci_cli.verify_versions(self.pins)

    @patch('verify_oci_cli.subprocess.check_output', return_value='0.0.0\n')
    @patch('verify_oci_cli.metadata.version')
    def test_rejects_wrong_cli_on_path(self, version, cli):
        version.side_effect = self.pins.__getitem__
        with self.assertRaisesRegex(RuntimeError, 'OCI CLI version mismatch'):
            verify_oci_cli.verify_versions(self.pins)

    @patch('verify_oci_cli.subprocess.check_output', side_effect=subprocess.CalledProcessError(7, 'oci'))
    @patch('verify_oci_cli.metadata.version')
    def test_rejects_cli_execution_failure(self, version, cli):
        version.side_effect = self.pins.__getitem__
        with self.assertRaises(subprocess.CalledProcessError):
            verify_oci_cli.verify_versions(self.pins)


if __name__ == '__main__':
    unittest.main()
