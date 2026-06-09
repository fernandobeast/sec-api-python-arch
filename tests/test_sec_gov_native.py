import importlib.util
import sys
import types
import unittest
from pathlib import Path

if "requests" not in sys.modules:
    fake_requests = types.ModuleType("requests")
    fake_requests.Session = lambda: None
    sys.modules["requests"] = fake_requests

MODULE_PATH = Path(__file__).resolve().parents[1] / "sec_api" / "sec_gov.py"
spec = importlib.util.spec_from_file_location("sec_gov_native", MODULE_PATH)
sec_gov_native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sec_gov_native)

SecGovEquityApi = sec_gov_native.SecGovEquityApi
_compact_recent_filings = sec_gov_native._compact_recent_filings
filing_url = sec_gov_native.filing_url
normalize_cik = sec_gov_native.normalize_cik


class FakeClient:
    def __init__(self):
        self.urls = []

    def get_json(self, url, params=None):
        self.urls.append((url, params))
        if url.endswith("company_tickers_exchange.json"):
            return {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
            }
        if "/api/xbrl/companyfacts/" in url:
            return {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "dei": {
                        "EntityCommonStockSharesOutstanding": {
                            "label": "Entity Common Stock, Shares Outstanding",
                            "units": {
                                "shares": [
                                    {
                                        "end": "2024-09-28",
                                        "val": 15115823000,
                                        "accn": "0000320193-24-000123",
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    }
                                ]
                            },
                        },
                        "EntityPublicFloat": {"units": {"USD": [{"val": 100, "filed": "2024-01-01"}]}},
                    },
                    "us-gaap": {
                        "Revenues": {
                            "label": "Revenue",
                            "description": "Revenue from contracts with customers.",
                            "units": {"USD": [{"end": "2024-09-28", "val": 391035000000, "form": "10-K", "filed": "2024-11-01"}]},
                        }
                    },
                },
            }
        if "/submissions/" in url:
            return {
                "cik": "0000320193",
                "name": "Apple Inc.",
                "tickers": ["AAPL"],
                "exchanges": ["Nasdaq"],
                "formerNames": [{"name": "APPLE COMPUTER INC", "from": "1994-01-26", "to": "2007-01-09"}],
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-24-000123", "0000320193-24-000100"],
                        "filingDate": ["2024-11-01", "2024-08-01"],
                        "reportDate": ["2024-09-28", "2024-07-01"],
                        "acceptanceDateTime": ["20241101120000", "20240801120000"],
                        "act": ["34", "34"],
                        "form": ["10-K", "8-K"],
                        "fileNumber": ["001-36743", "001-36743"],
                        "filmNumber": ["", ""],
                        "items": ["", "2.02"],
                        "size": [100, 200],
                        "isXBRL": [1, 1],
                        "isInlineXBRL": [1, 1],
                        "primaryDocument": ["aapl-20240928.htm", "aapl-20240801.htm"],
                        "primaryDocDescription": ["10-K", "8-K"],
                    }
                },
            }
        raise AssertionError("unexpected URL " + url)


class SecGovNativeTests(unittest.TestCase):
    def test_normalize_cik(self):
        self.assertEqual(normalize_cik("320193"), "0000320193")

    def test_compacts_recent_filings_with_archive_urls(self):
        rows = _compact_recent_filings(
            {
                "cik": [320193],
                "accessionNumber": ["0000320193-24-000123"],
                "filingDate": ["2024-11-01"],
                "form": ["10-K"],
                "primaryDocument": ["aapl-20240928.htm"],
            }
        )
        self.assertEqual(rows[0]["reportUrl"], filing_url(320193, "0000320193-24-000123", "aapl-20240928.htm"))

    def test_query_filings_filters_us_equities_and_forms(self):
        api = SecGovEquityApi(client=FakeClient())
        result = api.important_events(ticker="AAPL")
        self.assertEqual(result["filings"][0]["form"], "8-K")

    def test_financial_statement_and_capital_structure(self):
        api = SecGovEquityApi(client=FakeClient())
        statement = api.financial_statement(ticker="AAPL")
        capital = api.capital_structure(ticker="AAPL")
        self.assertEqual(statement["statements"]["income_statement"]["Revenues"]["facts"][0]["val"], 391035000000)
        self.assertEqual(capital["metrics"]["shares_outstanding"]["latest"]["val"], 15115823000)


if __name__ == "__main__":
    unittest.main()
