"""The canary's memory hogs, as command text. The hog itself runs only on a worker.

On 2026-10-05 the `file` hog read `/work/node_modules`, a golden without one gave
it nothing to read, and the canary's three oom checks failed. These pin down that
the file hogs bring their own working set and no longer depend on the repository.
"""
import unittest

from pandora.executor import memtest


class FileHog(unittest.TestCase):
    def script(self, name):
        argv = memtest.hog(name)
        self.assertEqual(argv[:2], ['bash', '-c'])
        return argv[2]

    def test_file_is_the_default(self):
        self.assertEqual(memtest.DEFAULT, 'file')
        self.assertEqual(memtest.hog(), memtest.hog('file'))

    def test_no_file_hog_reads_the_repository(self):
        for name in ('file', 'mixed'):
            script = self.script(name)
            self.assertNotIn('node_modules', script, name)
            self.assertNotIn('find ', script, name)

    def test_the_working_set_is_written_before_it_is_read(self):
        for name in ('file', 'mixed'):
            script = self.script(name)
            self.assertTrue(script.startswith(memtest.WORKING_SET), name)
            self.assertLess(script.index('/dev/urandom'), script.index('while :'), name)
            self.assertTrue(script.endswith(memtest.THRASH), name)

    def test_the_working_set_is_several_times_the_canary_ceiling(self):
        size_mib = memtest.WORKING_SET_FILES * memtest.WORKING_SET_MIB
        self.assertGreaterEqual(size_mib, 3 * 512)
        self.assertIn('seq 1 %d' % memtest.WORKING_SET_FILES, memtest.WORKING_SET)
        self.assertIn('count=%d' % memtest.WORKING_SET_MIB, memtest.WORKING_SET)

    def test_the_working_set_is_on_disk_not_tmpfs(self):
        # /tmp is tmpfs on Ubuntu: its pages are shmem, unreclaimable without
        # swap, so a working set there would be an anon hog.
        self.assertTrue(memtest.WORKING_SET_DIR.startswith('/work/'))
        self.assertNotIn('/tmp', memtest.WORKING_SET)

    def test_writing_bypasses_the_page_cache_with_a_fallback(self):
        self.assertIn('oflag=direct', memtest.WORKING_SET)
        self.assertIn('|| dd ', memtest.WORKING_SET)
        self.assertIn('conv=fsync', memtest.WORKING_SET)

    def test_the_thrash_reads_the_working_set_forever(self):
        self.assertEqual(memtest.THRASH,
                         'while :; do cat %s/* > /dev/null 2>&1; done' % memtest.WORKING_SET_DIR)

    def test_mixed_starts_anonymous_growth_after_the_working_set(self):
        script = self.script('mixed')
        self.assertLess(script.index('done; '), script.index('node -e'))
        self.assertIn('fill(1)', script)


class Lookup(unittest.TestCase):
    def test_each_call_is_a_fresh_list(self):
        first = memtest.hog('file')
        first.append('x')
        self.assertNotIn('x', memtest.hog('file'))

    def test_an_unknown_hog_names_the_known_ones(self):
        with self.assertRaises(KeyError) as caught:
            memtest.hog('swap')
        self.assertIn('file', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
