"""Explicit test selection avoids collecting inherited fixtures repeatedly."""
import argparse
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', action='store_true', help='also exercise an allocated idle CUDA GPU')
    parser.add_argument('--report', help='write a concise JSON test report')
    args = parser.parse_args()
    from test_client import ClientTest
    from test_dialogue import DialogueTest, HeartbeatRulesTest
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for cls in [ClientTest, DialogueTest, HeartbeatRulesTest]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    if sys.platform.startswith('linux'):
        from test_robustness import RobustnessTest
        from test_integration import IntegrationTest
        from test_meeting import MeetingTest
        suite.addTests(loader.loadTestsFromTestCase(RobustnessTest))
        suite.addTests(IntegrationTest(name) for name in IntegrationTest.__dict__ if name.startswith('test_'))
        suite.addTests(MeetingTest(name) for name in MeetingTest.__dict__ if name.startswith('test_'))
    if args.gpu:
        if not sys.platform.startswith('linux'):
            parser.error('--gpu requires Linux')
        from test_gpu import CudaTest
        from test_pulse_gpu import PulseTest
        for cls in [CudaTest, PulseTest]:
            suite.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=len(result.skipped), seconds=round(time.monotonic()-started, 2),
                  platform=sys.platform, python=sys.version.split()[0], gpu_tests=args.gpu,
                  success=result.wasSuccessful())
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2)+'\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
