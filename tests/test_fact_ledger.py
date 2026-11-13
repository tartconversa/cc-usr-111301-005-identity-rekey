"""不可变事实账本测试：来源身份不覆盖、重送、冲突与禁止按金额去重。"""
import unittest

from src.fact_ledger import FactConflict, FactLedger


class FactLedgerTests(unittest.TestCase):
    def _ledger(self):
        ledger = FactLedger()
        ledger.load_rows([
            {"fact_id": "f1", "source_alias": "a", "usage": "2"},
            {"fact_id": "f2", "source_alias": "b", "usage": "3"},
            {"fact_id": "f3", "source_alias": "c", "usage": "5"},
        ])
        return ledger

    def test_facts_keep_one_row_each_with_source_alias(self):
        ledger = self._ledger()
        self.assertEqual([fact.fact_id for fact in ledger.facts],
                         ["f1", "f2", "f3"])
        self.assertEqual(
            [(fact.source_alias, fact.usage) for fact in ledger.facts],
            [("a", 2), ("b", 3), ("c", 5)],
        )
        self.assertEqual(ledger.total_usage(), 10)

    def test_equal_amount_independent_facts_are_not_deduplicated(self):
        ledger = FactLedger()
        ledger.add({"fact_id": "x1", "source_alias": "a", "usage": 7})
        ledger.add({"fact_id": "x2", "source_alias": "b", "usage": 7})
        # 金额相同也必须保留两行，合并时用量相加而非去重。
        self.assertEqual(len(ledger.facts), 2)
        self.assertEqual(ledger.total_usage(), 14)

    def test_identical_resend_is_idempotent(self):
        ledger = FactLedger()
        ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 2})
        ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 2})
        self.assertEqual(len(ledger.facts), 1)
        self.assertEqual(ledger.resends, 1)

    def test_revised_fact_same_id_is_rejected_not_overwritten(self):
        ledger = FactLedger()
        ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 2})
        with self.assertRaises(FactConflict):
            # 真实事实修订（用量被改）必须显式拒绝，不能静默覆盖。
            ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 9})
        with self.assertRaises(FactConflict):
            # 来源身份被改同样拒绝。
            ledger.add({"fact_id": "f1", "source_alias": "z", "usage": 2})
        original = ledger.get("f1")
        self.assertEqual((original.source_alias, original.usage), ("a", 2))
        self.assertEqual(ledger.rejected_conflicts, 2)

    def test_blank_usage_distinct_from_zero(self):
        ledger = FactLedger()
        ledger.add({"fact_id": "f0", "source_alias": "a", "usage": "0"})
        self.assertEqual(ledger.get("f0").usage, 0)
        with self.assertRaises(FactConflict):
            ledger.add({"fact_id": "f9", "source_alias": "a", "usage": ""})

    def test_negative_and_non_integer_usage_rejected(self):
        ledger = FactLedger()
        with self.assertRaises(FactConflict):
            ledger.add({"fact_id": "f1", "source_alias": "a", "usage": -1})
        with self.assertRaises(FactConflict):
            ledger.add({"fact_id": "f2", "source_alias": "a", "usage": "1.5"})


if __name__ == "__main__":
    unittest.main()
