# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The node pool initialization script and its runbook (docs/node-pool.md)."""

import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/oke/node-pool-init.sh"
RUNBOOK = ROOT / "docs/node-pool.md"
# Oracle's "Using custom cloud-init scripts", example 5. The first and last lines
# are the default OKE logic, which must not be modified.
FETCH = ('curl --fail -H "Authorization: Bearer Oracle" -L0 '
         'http://169.254.169.254/opc/v2/instance/metadata/oke_init_script '
         '| base64 --decode >/var/run/oke-init.sh')
GROWFS = "bash /usr/libexec/oci-growfs -y"
OKE_INIT = "bash /var/run/oke-init.sh"
OKE_INIT_PATH = "/var/run/oke-init.sh"
GROWFS_PATH = "/usr/libexec/oci-growfs"


def commands(text):
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def write_executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ScriptTests(unittest.TestCase):
    text = SCRIPT.read_text()

    def test_shebang_and_license_header(self):
        lines = self.text.splitlines()
        # cloud-init runs user_data as a script only when it starts with a shebang.
        self.assertEqual(lines[0], "#!/bin/bash")
        self.assertEqual(lines[1:3], ["# Copyright (c) 2026 The Jaeger Authors.",
                                      "# SPDX-License-Identifier: Apache-2.0"])

    def test_commands_are_oracle_example_5_verbatim(self):
        self.assertEqual(commands(self.text), [FETCH, GROWFS, OKE_INIT])

    def test_a_growfs_failure_does_not_stop_registration(self):
        # The chosen failure semantics: no errexit, so the OKE init script runs
        # even when oci-growfs fails (docs/node-pool.md#the-initialization-script).
        code = "\n".join(commands(self.text))
        self.assertNotRegex(code, r"\bset\s+-[a-zA-Z]*e")
        self.assertNotRegex(code, r"\bset\s+-o\s+(errexit|pipefail)")
        self.assertNotRegex(code, r"\btrap\b|\bexit\b|\|\|")
        self.assertNotRegex(self.text.splitlines()[0], r"\s-")

    def test_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def run_script(self, growfs_status=0, oke_status=0, growfs_present=True):
        """Run the script with the metadata service, oci-growfs and paths stubbed."""
        self.assertEqual(self.text.count(GROWFS_PATH), 1)
        self.assertEqual(self.text.count(OKE_INIT_PATH), 2)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log = tmp / "calls.log"
            (tmp / "bin").mkdir()
            oke_init = f'#!/bin/bash\necho oke-init "$@" >>"{log}"\nexit {oke_status}\n'
            write_executable(tmp / "bin/curl", (
                "#!/bin/bash\n"
                f'printf "%s\\n" "$@" >"{tmp}/curl-args"\n'
                f"printf '%s' '{base64.b64encode(oke_init.encode()).decode()}'\n"))
            if growfs_present:
                write_executable(tmp / "oci-growfs",
                                 f'#!/bin/bash\necho growfs "$@" >>"{log}"\nexit {growfs_status}\n')
            script = tmp / "node-pool-init.sh"
            script.write_text(self.text.replace(GROWFS_PATH, str(tmp / "oci-growfs"))
                              .replace(OKE_INIT_PATH, str(tmp / "oke-init.sh")))
            env = dict(os.environ, PATH=f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            calls = log.read_text().splitlines() if log.exists() else []
            curl_args = (tmp / "curl-args").read_text().splitlines()
        self.assertEqual(curl_args, ["--fail", "-H", "Authorization: Bearer Oracle", "-L0",
                                     "http://169.254.169.254/opc/v2/instance/metadata/oke_init_script"])
        return result.returncode, calls

    def test_growfs_runs_before_the_oke_init_script(self):
        self.assertEqual(self.run_script(), (0, ["growfs -y", "oke-init"]))

    def test_the_node_registers_when_growfs_fails(self):
        self.assertEqual(self.run_script(growfs_status=1), (0, ["growfs -y", "oke-init"]))

    def test_the_node_registers_when_growfs_is_missing(self):
        self.assertEqual(self.run_script(growfs_present=False), (0, ["oke-init"]))

    def test_an_oke_init_failure_is_the_script_status(self):
        self.assertEqual(self.run_script(oke_status=3), (3, ["growfs -y", "oke-init"]))

    def test_shellcheck_covers_the_script_locally_and_in_ci(self):
        makefile = (ROOT / "Makefile").read_text()
        lint = re.search(r"^lint:\n\tshellcheck ([^\n]*)", makefile, re.M)
        self.assertIsNotNone(lint)
        self.assertIn("ops/oke/*.sh", lint.group(1).split())
        steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["tests"]["steps"]
        runs = [s["run"] for s in steps if s.get("name") == "Lint shell scripts"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].split(), ["shellcheck"] + lint.group(1).split())


FIXTURE_OCIDS = ["ocid1.nodepool.oc1..examplepool", "ocid1.image.oc1..exampleimage"]


def node_pool(metadata):
    data = {
        "id": FIXTURE_OCIDS[0],
        "name": "example-pool",
        "kubernetes-version": "v1.37.0",
        "node-shape": "VM.Standard.E4.Flex",
        "node-config-details": {"size": 1},
        "node-source-details": {"source-type": "IMAGE", "image-id": FIXTURE_OCIDS[1],
                                "boot-volume-size-in-gbs": 100},
    }
    if metadata is not None:
        data["node-metadata"] = metadata
    return {"data": data, "etag": "example-etag-1"}


# Records every call and answers `get` from the fixture; `update` keeps a copy of
# the metadata file it was given, because the runbook deletes it afterwards.
OCI_STUB = r"""#!/bin/bash
call="$(mktemp "$OCI_CALLS/call.XXXXXX")"
printf '%s\0' "$@" >"$call"
case "$1 $2 $3" in
  'ce node-pool get') cat "$OCI_FIXTURE" ;;
  'ce node-pool update')
    prev=''
    for arg in "$@"; do
      if [[ $prev == --node-metadata ]]; then cp "${arg#file://}" "$OCI_CALLS/metadata.json"; fi
      prev="$arg"
    done
    echo '{"opc-work-request-id": "example-work-request"}' ;;
  *) exit 2 ;;
esac
"""


class RunbookTests(unittest.TestCase):
    blocks = re.findall(r"^```bash\n(.*?)^```$", RUNBOOK.read_text(), re.M | re.S)

    def block(self, marker):
        found = [b for b in self.blocks if marker in b]
        self.assertEqual(len(found), 1, marker)
        return found[0]

    def run_blocks(self, code, metadata):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ("bin", "calls", "work"):
                (tmp / name).mkdir()
            write_executable(tmp / "bin/oci", OCI_STUB)
            fixture = tmp / "node-pool.json"
            fixture.write_text(json.dumps(node_pool(metadata)))
            env = dict(os.environ, PATH=f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
                       OCI_CALLS=str(tmp / "calls"), OCI_FIXTURE=str(fixture), TMPDIR=str(tmp / "work"))
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [(tmp / "calls" / f).read_text().split("\0")[:-1]
                     for f in os.listdir(tmp / "calls") if f.startswith("call.")]
            sent = tmp / "calls/metadata.json"
            sent = json.loads(sent.read_text()) if sent.exists() else None
            leftovers = os.listdir(tmp / "work")
        for secret in FIXTURE_OCIDS + ["existing-secret-value", "echo secret"]:
            self.assertNotIn(secret, result.stdout)
        return result.stdout, calls, sent, leftovers

    def update(self, metadata):
        code = self.block("jq --arg user_data") + self.block("oci ce node-pool update")
        stdout, calls, sent, leftovers = self.run_blocks(code, metadata)
        self.assertEqual(leftovers, [])
        updates = [c for c in calls if c[:3] == ["ce", "node-pool", "update"]]
        self.assertEqual(len(updates), 1)
        self.assertTrue(all(c[:3] in (["ce", "node-pool", "get"], ["ce", "node-pool", "update"]) for c in calls))
        args = updates[0][3:]
        self.assertEqual([a for a in args if a.startswith("--")],
                         ["--node-pool-id", "--node-metadata", "--if-match", "--force",
                          "--wait-for-state", "--wait-for-state"])
        self.assertEqual(args[args.index("--node-pool-id") + 1], "<node-pool-ocid>")
        self.assertEqual(args[args.index("--if-match") + 1], "example-etag-1")
        self.assertTrue(args[args.index("--node-metadata") + 1].startswith("file://"))
        self.assertEqual(base64.b64decode(sent["user_data"], validate=True), SCRIPT.read_bytes())
        return stdout, sent

    def test_update_keeps_other_metadata_and_sets_the_committed_script(self):
        stdout, sent = self.update({"existing-key": "existing-secret-value"})
        self.assertEqual(set(sent), {"existing-key", "user_data"})
        self.assertEqual(sent["existing-key"], "existing-secret-value")
        self.assertEqual(json.loads(stdout[:stdout.index("]") + 1]), ["existing-key", "user_data"])

    def test_update_without_metadata_sets_only_the_script(self):
        _, sent = self.update(None)
        self.assertEqual(set(sent), {"user_data"})

    def test_update_replaces_an_earlier_value(self):
        old = base64.b64encode(b"#!/bin/bash\necho secret\n").decode()
        _, sent = self.update({"user_data": old})
        self.assertEqual(set(sent), {"user_data"})

    def inspect(self, metadata):
        stdout, calls, _, _ = self.run_blocks(self.block("cmp -s - ops/oke/node-pool-init.sh"), metadata)
        self.assertTrue(calls)
        self.assertTrue(all(c[:3] == ["ce", "node-pool", "get"] for c in calls))
        summary = json.loads(stdout[:stdout.rindex("}") + 1])
        self.assertEqual(summary["boot_volume_size_in_gbs"], 100)
        return stdout.splitlines()[-1], summary["node_metadata_keys"]

    def test_inspection_recognizes_the_committed_script(self):
        user_data = base64.b64encode(SCRIPT.read_bytes()).decode()
        self.assertEqual(self.inspect({"user_data": user_data, "existing-key": "existing-secret-value"}),
                         ("user_data is ops/oke/node-pool-init.sh", ["existing-key", "user_data"]))

    def test_inspection_reports_a_missing_script(self):
        self.assertEqual(self.inspect(None),
                         ("user_data is not set or differs from ops/oke/node-pool-init.sh", []))

    def test_inspection_reports_another_script_without_printing_it(self):
        other = base64.b64encode(b"#!/bin/bash\necho secret\n").decode()
        self.assertEqual(self.inspect({"user_data": other}),
                         ("user_data is not set or differs from ops/oke/node-pool-init.sh", ["user_data"]))


if __name__ == "__main__":
    unittest.main()
