"""Pure AST-extracted helper checks; no package import or native execution."""
import ast
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'tools/_review_dependency_case_inputs.py').read_text())
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'NativeCaseInputs')
CLAIMS = frozenset({'observed-capacity', 'effective-capacity', 'observed-demand', 'adequacy', 'margin', 'independent-note'})

def require(value, reason):
    if not value:
        raise ValueError(reason)

NAMES = {'_refresh_expiry_reviews', '_review_expiry_observation'}
SELECTED = [n for n in CLASS.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
NAMESPACE = dict(CLAIM_IDS=CLAIMS, datetime=datetime, timedelta=timedelta, _require=require)
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Toy', bases=[], keywords=[], body=SELECTED, decorator_list=[])], type_ignores=[])), '<pure helpers>', 'exec'), NAMESPACE)  # noqa: S102 - isolate selected trusted local methods without runtime imports.
Toy = NAMESPACE['Toy']

class ControlledClockTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.toy = Toy()
        self.toy.applications = {}
        for claim in CLAIMS:
            for config in ('evidence', 'critic'):
                cycle = 'baseline' if claim == 'independent-note' else 'review-age-refresh'
                at = self.now + timedelta(seconds=10 if cycle == 'baseline' else 30)
                self.toy.applications[(cycle, claim, config)] = {'envelope': {'payload': {'body': {'review': {'reviewed_at': at.isoformat()}}}, 'authentication': {'issued_at': (at + timedelta(seconds=1)).isoformat(), 'signature': 'toy-only'}}}

    def test_forward_interval_and_unmodified_envelopes(self):
        before = copy.deepcopy(self.toy.applications)
        target = self.toy._review_expiry_observation(600, self.now + timedelta(seconds=40), self.now + timedelta(seconds=39))
        self.assertEqual(target, self.now + timedelta(seconds=610, microseconds=1))
        for (cycle, _, _), application in self.toy.applications.items():
            age = (target - datetime.fromisoformat(application['envelope']['payload']['body']['review']['reviewed_at'])).total_seconds()
            self.assertEqual(age > 600, cycle == 'baseline')
        self.assertEqual(before, self.toy.applications)

    def test_no_backdating(self):
        with self.assertRaisesRegex(ValueError, 'interval_unavailable'):
            self.toy._review_expiry_observation(600, self.now + timedelta(seconds=631), self.now)

    def test_last_observation_is_lower_bound(self):
        target = self.toy._review_expiry_observation(600, self.now, self.now + timedelta(seconds=620))
        self.assertEqual(target, self.now + timedelta(seconds=620))

    def test_wrong_policy_fails(self):
        for maximum in (599, 601, 86400):
            with self.assertRaisesRegex(ValueError, 'fixture_not_selected'):
                self.toy._review_expiry_observation(maximum, self.now, self.now)

    def test_timestamp_after_signature_fails(self):
        app = next(iter(self.toy.applications.values()))
        app['envelope']['authentication']['issued_at'] = self.now.isoformat()
        with self.assertRaisesRegex(ValueError, 'timestamp_after_signature'):
            self.toy._review_expiry_observation(600, self.now, self.now)

    def test_real_refresh_path_requested_without_launch(self):
        calls = []
        self.toy.drive = lambda *args, **kwargs: calls.append((args, kwargs))
        self.toy._refresh_expiry_reviews()
        self.assertEqual(calls, [((CLAIMS - {'independent-note'},), {'cycle': 'review-age-refresh'})])
        drive_tree = ast.parse((ROOT / 'tools/_review_dependency_cases.py').read_text())
        drive = next(n for n in ast.walk(drive_tree) if isinstance(n, ast.FunctionDef) and n.name == 'drive')
        defaults = dict(zip([a.arg for a in drive.args.kwonlyargs], drive.args.kw_defaults, strict=True))
        self.assertEqual(ast.literal_eval(defaults['configs']), ('evidence', 'critic'))
        self.assertEqual(12 + 5 * len(ast.literal_eval(defaults['configs'])), 22)

    def test_other_temporal_paths_preserved(self):
        text = ast.unparse(TREE)
        for reason in ('host_state_clock_regressed', 'strict_source_expired', 'strict_source_outside_effective_interval'):
            self.assertIn(reason, text)
        self.assertIn('expired == {\'independent-note\'}', text)
        self.assertIn('_accepted(strict) == CLAIM_IDS - affected', text)
        self.assertIn('self.guard() == opening', text)

if __name__ == '__main__':
    unittest.main(verbosity=2)
