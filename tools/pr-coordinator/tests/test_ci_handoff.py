"""SF-145: workers hand pending hosted CI to the coordinator instead of polling it (offline)."""
import unittest

from test_ci_redirects import Fixture, c


class CIHandoff(Fixture):
    def test_implementation_prompt_hands_off_pending_ci(self):
        prompt = self.worker.prompt(self.project, self.claim, self.state/'run')
        self.assertIn(c.CI_HANDOFF, prompt)
        self.assertNotIn('wait for current-head CI', prompt)
        self.assertIn('Pending CI is never approval', prompt)

    def test_repair_prompt_hands_off_pending_ci_but_review_gates_stay(self):
        engine = c.Coordinator({**self.config, 'workers': {'Codex': ['x'], 'Claude': ['y']}}, self.state)
        pr = {**self.pr, 'isDraft': False}
        fix = engine.prompt(self.project, pr, self.owner, 'fix', 'Codex', self.state, self.state/'result.json')
        self.assertIn(c.CI_HANDOFF, fix)
        self.assertNotIn('verifying the pushed head and required checks', fix)
        review = engine.prompt(self.project, pr, self.owner, 'review', 'Claude', self.state, self.state/'result.json')
        self.assertNotIn(c.CI_HANDOFF, review)
        self.assertIn('SOURCE REVIEW WHILE CI IS UNREADY', review)
        self.assertIn('This is not merge approval', review)


if __name__ == '__main__':
    unittest.main()
