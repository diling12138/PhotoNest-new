"""Named-object checks use unique temporary library names, without desktop UI."""
import os
from pathlib import Path
import tempfile
import unittest

if os.name == 'nt':
    from desktop import SessionInstance


@unittest.skipUnless(os.name == 'nt', 'Windows named mutex/event required')
class SessionInstanceTests(unittest.TestCase):
    def test_second_launch_signals_existing_library_and_close_releases_session(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-session-') as folder:
            first = SessionInstance(folder)
            second = other = None
            try:
                self.assertFalse(first.already_running)
                self.assertFalse(first.consume_open())
                second = SessionInstance(str(Path(folder)/'.'))
                self.assertTrue(second.already_running)
                second.request_open()
                self.assertTrue(first.consume_open())
                self.assertFalse(first.consume_open(), 'The open request should be consumed once')
                other = SessionInstance(str(Path(folder)/'different library'))
                self.assertFalse(other.already_running, 'Different libraries must not share the session lock')
            finally:
                for instance in (other, second, first):
                    if instance is not None:
                        instance.close()
            reopened = SessionInstance(folder)
            try:
                self.assertFalse(reopened.already_running)
                self.assertFalse(reopened.consume_open())
            finally:
                reopened.close()
                reopened.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
