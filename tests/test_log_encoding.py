"""Exercise the actual launch options with bytes that killed the Windows reader."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest


class LogEncodingTests(unittest.TestCase):
    def test_worker_output_survives_unicode_and_invalid_bytes(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8'))
        launches = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == 'Popen']
        self.assertEqual(len(launches), 2)
        for launch in launches:
            options = {k.arg: ast.literal_eval(k.value) for k in launch.keywords
                       if k.arg in ('text', 'encoding', 'errors', 'bufsize')}
            with subprocess.Popen([sys.executable, '-c',
                 "import sys; sys.stdout.buffer.write(('quote: \\u201d emoji: \\U0001f600\\n').encode('utf-8') + b'bad: \\x9d\\nPROGRESS scan 100\\n')"],
                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options) as proc:
                lines = list(proc.stdout)
            self.assertEqual(proc.returncode, 0)
            self.assertIn('\u201d', lines[0])
            self.assertIn('\ufffd', lines[1])
            self.assertEqual(lines[-1].strip(), 'PROGRESS scan 100')


if __name__ == '__main__':
    unittest.main()
