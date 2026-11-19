"""Print how many test cases ran (not skipped) in a JUnit report; with --skipped, how many were skipped."""

import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()  # noqa: S314 - our own pytest report, written one step earlier
suites = [root] if root.tag == "testsuite" else list(root)
skipped = sum(int(s.get("skipped", 0)) for s in suites)
print(skipped if "--skipped" in sys.argv[2:] else sum(int(s.get("tests", 0)) for s in suites) - skipped)
