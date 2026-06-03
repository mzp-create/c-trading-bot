"""Reproduce the Bitfinex on-req parsing bug and verify the recovery fix.

Root cause (captured live 2026-06-03): ccxt 4.5.54 cannot parse Bitfinex's
`on-req` order-submit acknowledgement and returns an all-None order, so
create_order saw no id even though the order was accepted and filled. That
false-negative caused open orders to be retried and stacked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data.bitfinex_client import BitfinexClient

# The exact on-req array Bitfinex returned (CCXT could not parse this).
ONREQ_SUCCESS = [
    1780522756, "on-req", None, None,
    [[238126853200, None, 1780522756239, "tBTCUST", 1780522756240, 1780522756240,
      0.00018344, 0.00018344, "MARKET", None, None, None, 0, "ACTIVE", None, None,
      65415, 0, 0, 0, None, None, None, 0, 0, None, None, None, "API>BFX", None, None,
      {"source": "api"}]],
    None, "SUCCESS", "Submitting 1 orders.",
]
ONREQ_ERROR = [
    1780522756, "on-req", None, None, None, None, "ERROR",
    "not enough balance for tBTCUST",
]
# What CCXT hands back for an unparsed on-req: an all-None order.
EMPTY_CCXT_ORDER = {"id": None, "filled": None, "average": None, "status": None,
                    "symbol": None, "amount": None}


class TestOnReqRecovery(unittest.TestCase):
    def setUp(self):
        # Paper mode so __init__ does not need API keys; we call helpers directly.
        cfg = {"exchange": {"name": "bitfinex"}, "trading": {"initial_capital": 100}}
        self.client = BitfinexClient(cfg, mode="paper")

    def test_parse_onreq_success(self):
        info = self.client._parse_onreq(ONREQ_SUCCESS)
        self.assertIsNotNone(info)
        self.assertEqual(str(info["id"]), "238126853200")
        self.assertEqual(info["status"], "ACTIVE")
        self.assertIsNone(info.get("error"))

    def test_parse_onreq_error(self):
        info = self.client._parse_onreq(ONREQ_ERROR)
        self.assertIsNotNone(info)
        self.assertIsNone(info["id"])
        self.assertIn("not enough balance", info["error"])

    def test_recovered_order_reports_success_with_id(self):
        # Simulate: CCXT returned an empty order, but last_json_response holds on-req.
        order = dict(EMPTY_CCXT_ORDER)
        recovered = self.client._recover_order(order, ONREQ_SUCCESS)
        result = self.client._normalize_order_result(recovered, "BTC/USDT")
        self.assertTrue(result["success"], "accepted order must report success")
        self.assertEqual(result["id"], "238126853200")

    def test_recovered_error_reports_failure(self):
        order = dict(EMPTY_CCXT_ORDER)
        recovered = self.client._recover_order(order, ONREQ_ERROR)
        result = self.client._normalize_order_result(recovered, "BTC/USDT")
        self.assertFalse(result["success"], "rejected order must report failure")

    def test_bitfinex_to_display(self):
        self.assertEqual(self.client._bitfinex_to_display("tBTCUST"), "BTC/USDT")
        self.assertEqual(self.client._bitfinex_to_display("tBTCF0:USTF0"), "BTC/USDT")
        self.assertEqual(self.client._bitfinex_to_display("tETHUST"), "ETH/USDT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
