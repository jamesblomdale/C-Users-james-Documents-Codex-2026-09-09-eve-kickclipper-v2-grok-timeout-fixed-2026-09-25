import io,unittest
from contextlib import redirect_stdout
from export_status import finish_exports

class ExportStatusTests(unittest.TestCase):
    def test_failures_never_report_done(self):
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(RuntimeError):finish_exports(0,19)
        self.assertIn('0 clips produced, 19 failed',out.getvalue())
        self.assertNotIn('PROGRESS done',out.getvalue())

    def test_partial_failure_reports_actual_successes(self):
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(RuntimeError):finish_exports(3,19)
        self.assertIn('3 clips produced, 16 failed',out.getvalue())

    def test_success_reports_done(self):
        out=io.StringIO()
        with redirect_stdout(out):finish_exports(2,2)
        self.assertIn('PROGRESS done 100',out.getvalue())
