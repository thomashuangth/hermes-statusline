"""Review the README activation snippet without touching any live gateway.

Runs the shipped snippet with a stub `hermes --print-runtime-command` whose argv
matches the launcher shape observed in hermes_cli/_launchers.runtime_command
(`<python> -I -c <bootstrap> <args>`), and replaces subprocess.run so the
resulting command is inspected but never executed.
"""
import json
import pathlib
import runpy
import subprocess
import unittest

root = pathlib.Path(__file__).resolve().parents[1]
snippet = root / 'tests' / 'fixtures' / 'activation_snippet.py'

PINNED_ENTRY = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
BOOTSTRAP = ("import os, sys, runpy; sys.path.insert(0, '/opt/hermes'); "
             "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or '/home/u/.hermes'; "
             "import hermes_bootstrap; " + PINNED_ENTRY)


class FakeProcess:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = 0


class ActivationSnippetTest(unittest.TestCase):
    def run_snippet(self, argv):
        calls = {}

        def fake_check_output(cmd, text=False):
            self.assertEqual(cmd[0], 'hermes')
            return json.dumps(argv)

        def fake_run(cmd, check=False):
            calls['argv'] = cmd
            calls['check'] = check
            return FakeProcess(cmd)

        originals = (subprocess.check_output, subprocess.run)
        subprocess.check_output, subprocess.run = fake_check_output, fake_run
        try:
            runpy.run_path(str(snippet), run_name='__main__')
        finally:
            subprocess.check_output, subprocess.run = originals
        return calls

    def test_replaces_only_final_entry_and_never_executes(self):
        argv = ['/usr/bin/python3.14', '-I', '-c', BOOTSTRAP]
        calls = self.run_snippet(argv)
        sent = calls['argv']
        self.assertTrue(calls['check'])
        self.assertEqual(sent[:3], argv[:3])
        self.assertTrue(sent[2] == '-c')
        self.assertTrue(sent[3].startswith(BOOTSTRAP[:-len(PINNED_ENTRY)]))
        self.assertNotIn('run_module', sent[3])
        self.assertIn('exec(', sent[3])
        self.assertIn('from pathlib import Path', sent[3])
        self.assertIn('reload_gateway_plugins(home, profile_home=home)', sent[3])
        self.assertIn("result.get('reloaded') is not True", sent[3])
        # The bootstrap's own home pin and module path must survive verbatim.
        self.assertIn("sys.path.insert(0, '/opt/hermes')", sent[3])
        self.assertIn('import hermes_bootstrap', sent[3])

    def test_unexpected_launcher_shape_is_refused(self):
        argv = ['/usr/bin/python3.14', '-I', '-c', 'print("different launcher")']
        with self.assertRaises(BaseException):
            self.run_snippet(argv)


if __name__ == '__main__':
    unittest.main()
