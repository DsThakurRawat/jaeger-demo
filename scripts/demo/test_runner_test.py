# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Check dependency lookup when the runner changes to the repository root."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestRunnerTests(unittest.TestCase):
    def test_shunit2_is_available_to_suites_from_an_external_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            repository = directory / 'repository'
            runner = repository / 'scripts/utils/run-tests.sh'
            runner.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'scripts/utils/run-tests.sh', runner)
            (repository / 'scripts/demo').mkdir()
            (repository / 'scripts/demo/dependency_test.py').write_text('''import os
from pathlib import Path
import unittest

class DependencyTest(unittest.TestCase):
    def test_dependency_is_available(self):
        self.assertTrue((Path(os.environ['SHUNIT2']) / 'shunit2').is_file())
''')

            # Keep the real runner but replace the suites with dependency probes.
            suites = list(ROOT.glob('scripts/utils/*.test.sh')) + list(ROOT.glob('examples/otel-demo/*.test.sh'))
            for suite in suites:
                probe = repository / suite.relative_to(ROOT)
                probe.parent.mkdir(parents=True, exist_ok=True)
                probe.write_text('set -eu\nsource "$SHUNIT2/shunit2"\n')

            caller = directory / 'caller'
            dependency = caller / 'tools with spaces/shunit2'
            dependency.mkdir(parents=True)
            (dependency / 'shunit2').write_text('printf "dependency loaded\\n"\n')
            for path in (str(dependency.relative_to(caller)), str(dependency)):
                with self.subTest(shunit2=path):
                    result = subprocess.run(['bash', str(runner)], cwd=caller,
                                            env=dict(os.environ, SHUNIT2=path),
                                            text=True, capture_output=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(['dependency loaded'] * len(suites), result.stdout.splitlines())


if __name__ == '__main__':
    unittest.main()
