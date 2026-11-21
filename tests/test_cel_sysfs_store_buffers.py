"""Guard the sysfs store handlers of the release platforms' Celestica modules.

sysfs hands a store handler 'count' bytes followed by a NUL. A handler that
copies 'buf' with strcpy() into a 'count'-sized array writes one byte too
many, which FORTIFY_SOURCE turns into a kernel panic (#205: 'reboot' arms the
watchdog through setreg and panicked Seastone 2 and Silverstone-X). This only
reads the sources; the modules are built with the image.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIRS = (
    'platform/broadcom/sonic-platform-modules-cel/seastone2/modules',
    'platform/marvell-teralynx/sonic-platform-modules-cel/silverstone-x/modules',
)
# An array sized by the handler's length argument has no room for the NUL.
SHORT_ARRAY = re.compile(r'\bchar\s+\w+\s*\[\s*(count|size)\s*\]')
UNBOUNDED_COPY = re.compile(r'\bstrcpy\s*\(\s*\w+\s*,\s*buf\s*\)')


class CelSysfsStoreBufferTest(unittest.TestCase):
    def sources(self):
        files = [path for name in MODULE_DIRS for path in sorted((ROOT / name).glob('*.c'))]
        self.assertTrue(files, 'no module sources found, did the directories move?')
        return files

    def offenders(self, pattern):
        return [f'{path.relative_to(ROOT)}:{number}: {line.strip()}'
                for path in self.sources()
                for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1)
                if pattern.search(line)]

    def test_no_array_sized_without_room_for_the_nul(self):
        self.assertEqual(self.offenders(SHORT_ARRAY), [],
                         "size the copy of a sysfs buffer 'count + 1'")

    def test_no_unbounded_copy_of_the_sysfs_buffer(self):
        self.assertEqual(self.offenders(UNBOUNDED_COPY), [],
                         'copy a sysfs buffer with strscpy(dst, buf, sizeof(dst))')


if __name__ == '__main__':
    unittest.main()
