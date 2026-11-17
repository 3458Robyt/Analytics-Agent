from collections import Counter
import unittest

from scripts.validate_concepts import LIVE, PENDING, corpus, references


class ConceptCorpusTests(unittest.TestCase):
    def test_exact_question_and_lane_coverage(self):
        cases = corpus()
        self.assertEqual(list(range(1, 121)), [case.number for case in cases])
        self.assertEqual(120, len({case.question for case in cases}))
        self.assertEqual({'fixture': 84, 'live': 36}, Counter(case.lane for case in cases))
        self.assertTrue(all(case.criterion for case in cases))

    def test_live_oracles_cover_every_numeric_case(self):
        queries = references('America/Bogota', 'SELECT 1 AS prima_devengada')
        no_result = PENDING | {30}
        for case in corpus():
            if case.number in LIVE - no_result:
                self.assertIn(case.criterion, queries, case.number)
        self.assertIn('fecha_accidente', queries['acc_net'].sql)
        self.assertIn('cod_ramo_com = 202', queries['seq202'].sql)
        self.assertIn("DATE '2026-10-04'", queries['issued_cross'].sql)
