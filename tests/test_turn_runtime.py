"""Run the financial and conversation regressions through the actual graph driver."""
import io
import unittest
from dataclasses import replace
from unittest.mock import patch

from analytics_agent.agent import AnalyticsAgent


class TurnRuntimeParityTests(unittest.TestCase):
    def test_existing_agent_and_financial_regressions_through_both_engines(self):
        original = AnalyticsAgent.__init__
        for engine in ('legacy', 'langgraph'):
            with self.subTest(engine=engine):
                def initialize(agent, *args, **kwargs):
                    original(agent, *args, **kwargs)
                    agent.settings = replace(agent.settings, engine=engine)

                suite = unittest.defaultTestLoader.loadTestsFromNames([
                    'tests.test_agent', 'tests.test_analysis',
                    'tests.test_claims_agent', 'tests.test_workspace_features',
                ])
                output = io.StringIO()
                with patch.object(AnalyticsAgent, '__init__', initialize):
                    result = unittest.TextTestRunner(stream=output).run(suite)
                self.assertTrue(result.wasSuccessful(), output.getvalue())


if __name__ == '__main__':
    unittest.main()
