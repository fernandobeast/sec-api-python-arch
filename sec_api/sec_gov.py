"""Native SEC.gov EDGAR SDK for U.S.-listed equities.

This module talks directly to SEC.gov/data.sec.gov endpoints documented by the SEC:
submissions history, XBRL company facts/concepts/frames, ticker/exchange mappings,
Archives directory JSON indexes, and EDGAR Atom feeds.  It intentionally does not
require a sec-api.io token.
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
import requests


SEC_DATA_BASE_URL = "https://data.sec.gov"
SEC_WWW_BASE_URL = "https://www.sec.gov"
SEC_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CURRENT_ATOM_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

US_EQUITY_EXCHANGES = {
    "NASDAQ",
    "NYSE",
    "NYSE AMERICAN",
    "NYSE ARCA",
    "NYSE MKT",
    "AMEX",
    "OTC",
    "OTCQB",
    "OTCQX",
    "OTC PINK",
    "OTCMKTS",
}

DILUTION_FORMS = (
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "F-1",
    "F-1/A",
    "F-3",
    "F-3/A",
    "424B3",
    "424B4",
    "424B5",
)
IMPORTANT_EVENT_FORMS = ("8-K", "8-K/A", "6-K", "6-K/A")
INSIDER_FORMS = ("3", "3/A", "4", "4/A", "5", "5/A")
CORPORATE_ACTION_FORMS = ("8-K", "8-K/A", "6-K", "6-K/A", "10-Q", "10-K", "20-F")

CAPITAL_STRUCTURE_TAGS = {
    "shares_outstanding": (
        "dei",
        "EntityCommonStockSharesOutstanding",
    ),
    "public_float": (
        "dei",
        "EntityPublicFloat",
    ),
    "basic_weighted_average_shares": (
        "us-gaap",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ),
    "diluted_weighted_average_shares": (
        "us-gaap",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ),
    "common_stock_shares_authorized": (
        "us-gaap",
        "CommonStocksIncludingAdditionalPaidInCapitalSharesAuthorized",
    ),
    "common_stock_shares_issued": (
        "us-gaap",
        "CommonStocksIncludingAdditionalPaidInCapitalSharesIssued",
    ),
}

FINANCIAL_STATEMENT_TAGS = {
    "income_statement": {
        "Revenues": ("us-gaap", "Revenues"),
        "RevenueFromContractWithCustomerExcludingAssessedTax": (
            "us-gaap",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        ),
        "CostOfRevenue": ("us-gaap", "CostOfRevenue"),
        "GrossProfit": ("us-gaap", "GrossProfit"),
        "OperatingIncomeLoss": ("us-gaap", "OperatingIncomeLoss"),
        "NetIncomeLoss": ("us-gaap", "NetIncomeLoss"),
        "EarningsPerShareBasic": ("us-gaap", "EarningsPerShareBasic"),
        "EarningsPerShareDiluted": ("us-gaap", "EarningsPerShareDiluted"),
    },
    "balance_sheet": {
        "Assets": ("us-gaap", "Assets"),
        "AssetsCurrent": ("us-gaap", "AssetsCurrent"),
        "Liabilities": ("us-gaap", "Liabilities"),
        "LiabilitiesCurrent": ("us-gaap", "LiabilitiesCurrent"),
        "StockholdersEquity": ("us-gaap", "StockholdersEquity"),
        "CashAndCashEquivalentsAtCarryingValue": (
            "us-gaap",
            "CashAndCashEquivalentsAtCarryingValue",
        ),
    },
    "cash_flow_statement": {
        "NetCashProvidedByUsedInOperatingActivities": (
            "us-gaap",
            "NetCashProvidedByUsedInOperatingActivities",
        ),
        "NetCashProvidedByUsedInInvestingActivities": (
            "us-gaap",
            "NetCashProvidedByUsedInInvestingActivities",
        ),
        "NetCashProvidedByUsedInFinancingActivities": (
            "us-gaap",
            "NetCashProvidedByUsedInFinancingActivities",
        ),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "us-gaap",
            "PaymentsToAcquirePropertyPlantAndEquipment",
        ),
    },
}


class SecGovError(Exception):
    """Raised when SEC.gov returns an error response."""


class SecGovClient:
    """Low-level SEC.gov HTTP client with SEC-friendly headers and retries."""

    def __init__(
        self,
        user_agent=None,
        email=None,
        rate_limit_seconds=0.11,
        retries=3,
        proxies=None,
        session=None,
    ):
        if user_agent:
            agent = user_agent
        elif email:
            agent = "sec-api-python-arch/1.0 contact=" + email
        else:
            agent = "sec-api-python-arch/1.0 contact=you@example.com"

        self.session = session if session else requests.Session()
        self.session.headers.update(
            {
                "User-Agent": agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/xml, application/atom+xml, text/plain, */*",
            }
        )
        self.rate_limit_seconds = rate_limit_seconds
        self.retries = retries
        self.proxies = proxies if proxies else {}
        self._last_request_at = 0

    def _sleep_for_rate_limit(self):
        elapsed = time.time() - self._last_request_at
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)

    def request(self, method, url, **kwargs):
        response = None
        for attempt in range(self.retries):
            self._sleep_for_rate_limit()
            response = self.session.request(method, url, proxies=self.proxies, **kwargs)
            self._last_request_at = time.time()
            if response.status_code in (200, 201):
                return response
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise SecGovError(
                "SEC.gov error: {} - {}".format(response.status_code, response.text[:500])
            )
        raise SecGovError(
            "SEC.gov error: {} - {}".format(response.status_code, response.text[:500])
        )

    def get_json(self, url, params=None):
        response = self.request("GET", url, params=params)
        return response.json()

    def get_text(self, url, params=None):
        response = self.request("GET", url, params=params)
        return response.text


def normalize_cik(cik):
    """Return a CIK as the 10-digit string required by data.sec.gov."""
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise ValueError("CIK must contain digits")
    return digits.zfill(10)


def accession_without_dashes(accession_no):
    return str(accession_no).replace("-", "")


def _compact_recent_filings(recent):
    keys = list(recent.keys())
    if not keys:
        return []
    row_count = len(recent.get("accessionNumber", []))
    filings = []
    for idx in range(row_count):
        filing = {}
        for key in keys:
            values = recent.get(key, [])
            filing[key] = values[idx] if idx < len(values) else None
        filing["accessionNo"] = filing.get("accessionNumber")
        filing["filedAt"] = filing.get("filingDate")
        filing_cik = filing.get("cik") or filing.get("issuerCik")
        filing["reportUrl"] = filing_url(
            filing_cik, filing.get("accessionNumber"), filing.get("primaryDocument")
        )
        filing["filingDetailsUrl"] = filing_index_url(
            filing_cik, filing.get("accessionNumber")
        )
        filings.append(filing)
    return filings


def filing_index_url(cik, accession_no):
    if not cik or not accession_no:
        return None
    cik_int = str(int(str(cik)))
    return (
        SEC_ARCHIVES_BASE_URL
        + "/"
        + cik_int
        + "/"
        + accession_without_dashes(accession_no)
        + "/index.json"
    )


def filing_url(cik, accession_no, primary_document):
    if not cik or not accession_no or not primary_document:
        return None
    cik_int = str(int(str(cik)))
    return (
        SEC_ARCHIVES_BASE_URL
        + "/"
        + cik_int
        + "/"
        + accession_without_dashes(accession_no)
        + "/"
        + primary_document
    )


def _find_text_any(element, local_name, default=""):
    for child in element.iter():
        if child.tag.split("}")[-1] == local_name:
            return child.text or default
    return default


def _matches_form(form, allowed_forms):
    if not allowed_forms:
        return True
    normalized = {item.upper() for item in allowed_forms}
    return str(form).upper() in normalized


def _date_in_range(value, start_date=None, end_date=None):
    if not value:
        return False
    if start_date and value < start_date:
        return False
    if end_date and value > end_date:
        return False
    return True


def _latest_fact(units):
    candidates = []
    for unit_name, facts in units.items():
        for fact in facts:
            item = dict(fact)
            item["unit"] = unit_name
            candidates.append(item)
    candidates.sort(key=lambda fact: (fact.get("filed") or "", fact.get("end") or "", fact.get("fy") or 0, fact.get("fp") or ""), reverse=True)
    return candidates[0] if candidates else None


def _facts_by_period(units, form_types=None, limit=None):
    rows = []
    allowed = {form.upper() for form in form_types} if form_types else None
    for unit_name, facts in units.items():
        for fact in facts:
            if allowed and str(fact.get("form", "")).upper() not in allowed:
                continue
            item = dict(fact)
            item["unit"] = unit_name
            rows.append(item)
    rows.sort(key=lambda fact: (fact.get("end") or "", fact.get("filed") or ""), reverse=True)
    return rows[:limit] if limit else rows


class SecGovEquityApi:
    """High-level SDK for SEC.gov data about equities traded on U.S. exchanges."""

    def __init__(self, user_agent=None, email=None, allowed_exchanges=None, client=None, proxies=None):
        self.client = client if client else SecGovClient(user_agent=user_agent, email=email, proxies=proxies)
        self.allowed_exchanges = {exchange.upper() for exchange in (allowed_exchanges or US_EQUITY_EXCHANGES)}
        self._ticker_exchange_cache = None
        self._ticker_cache = None

    def get_company_tickers_exchange(self):
        if self._ticker_exchange_cache is None:
            data = self.client.get_json(SEC_COMPANY_TICKERS_EXCHANGE_URL)
            fields = data.get("fields", [])
            self._ticker_exchange_cache = [dict(zip(fields, row)) for row in data.get("data", [])]
        return self._ticker_exchange_cache

    def get_company_tickers(self):
        if self._ticker_cache is None:
            data = self.client.get_json(SEC_COMPANY_TICKERS_URL)
            if isinstance(data, dict):
                self._ticker_cache = list(data.values())
            else:
                self._ticker_cache = data
        return self._ticker_cache

    def resolve_ticker(self, ticker, require_us_exchange=True):
        ticker_upper = str(ticker).upper()
        matches = [row for row in self.get_company_tickers_exchange() if str(row.get("ticker", "")).upper() == ticker_upper]
        if require_us_exchange:
            matches = [row for row in matches if str(row.get("exchange", "")).upper() in self.allowed_exchanges]
        if not matches:
            raise ValueError("Ticker not found on allowed U.S. equity exchanges: " + ticker_upper)
        match = matches[0]
        match["cik_str"] = normalize_cik(match["cik"])
        return match

    def assert_us_equity_cik(self, cik):
        normalized = normalize_cik(cik)
        cik_int = int(normalized)
        matches = [row for row in self.get_company_tickers_exchange() if int(row.get("cik", 0)) == cik_int]
        allowed = [row for row in matches if str(row.get("exchange", "")).upper() in self.allowed_exchanges]
        if not allowed:
            raise ValueError("CIK is not mapped to an allowed U.S. equity exchange: " + normalized)
        return normalized

    def cik_for(self, ticker=None, cik=None, require_us_exchange=True):
        if ticker:
            return self.resolve_ticker(ticker, require_us_exchange=require_us_exchange)["cik_str"]
        if cik:
            return self.assert_us_equity_cik(cik) if require_us_exchange else normalize_cik(cik)
        raise ValueError("ticker or cik is required")

    def submissions(self, ticker=None, cik=None, require_us_exchange=True):
        cik_str = self.cik_for(ticker=ticker, cik=cik, require_us_exchange=require_us_exchange)
        data = self.client.get_json(SEC_DATA_BASE_URL + "/submissions/CIK" + cik_str + ".json")
        recent = dict(data.get("filings", {}).get("recent", {}))
        recent["cik"] = [int(cik_str)] * len(recent.get("accessionNumber", []))
        data.setdefault("filings", {})["recentExpanded"] = _compact_recent_filings(recent)
        return data

    def company_facts(self, ticker=None, cik=None, require_us_exchange=True):
        cik_str = self.cik_for(ticker=ticker, cik=cik, require_us_exchange=require_us_exchange)
        return self.client.get_json(SEC_DATA_BASE_URL + "/api/xbrl/companyfacts/CIK" + cik_str + ".json")

    def company_concept(self, taxonomy, tag, ticker=None, cik=None, require_us_exchange=True):
        cik_str = self.cik_for(ticker=ticker, cik=cik, require_us_exchange=require_us_exchange)
        path = "/api/xbrl/companyconcept/CIK{}/{}/{}.json".format(cik_str, taxonomy, tag)
        return self.client.get_json(SEC_DATA_BASE_URL + path)

    def frame(self, taxonomy, tag, unit, period):
        path = "/api/xbrl/frames/{}/{}/{}/{}.json".format(taxonomy, tag, unit, period)
        return self.client.get_json(SEC_DATA_BASE_URL + path)

    def financial_statement(self, ticker=None, cik=None, period_forms=("10-K", "10-Q", "20-F", "40-F"), limit_per_concept=8):
        facts = self.company_facts(ticker=ticker, cik=cik)
        output = {
            "cik": facts.get("cik"),
            "entityName": facts.get("entityName"),
            "statements": {},
        }
        all_facts = facts.get("facts", {})
        for statement_name, concepts in FINANCIAL_STATEMENT_TAGS.items():
            output["statements"][statement_name] = {}
            for label, (taxonomy, tag) in concepts.items():
                concept = all_facts.get(taxonomy, {}).get(tag)
                if concept:
                    output["statements"][statement_name][label] = {
                        "taxonomy": taxonomy,
                        "tag": tag,
                        "label": concept.get("label"),
                        "description": concept.get("description"),
                        "facts": _facts_by_period(concept.get("units", {}), form_types=period_forms, limit=limit_per_concept),
                    }
        return output

    def capital_structure(self, ticker=None, cik=None):
        facts = self.company_facts(ticker=ticker, cik=cik)
        all_facts = facts.get("facts", {})
        result = {
            "cik": facts.get("cik"),
            "entityName": facts.get("entityName"),
            "metrics": {},
        }
        for metric, (taxonomy, tag) in CAPITAL_STRUCTURE_TAGS.items():
            concept = all_facts.get(taxonomy, {}).get(tag)
            result["metrics"][metric] = {
                "taxonomy": taxonomy,
                "tag": tag,
                "latest": _latest_fact(concept.get("units", {})) if concept else None,
            }
        return result

    def query_filings(self, ticker=None, cik=None, form_types=None, start_date=None, end_date=None, size=100, require_us_exchange=True):
        cik_str = self.cik_for(ticker=ticker, cik=cik, require_us_exchange=require_us_exchange)
        data = self.client.get_json(SEC_DATA_BASE_URL + "/submissions/CIK" + cik_str + ".json")
        recent = data.get("filings", {}).get("recent", {})
        recent = dict(recent)
        recent["cik"] = [int(cik_str)] * len(recent.get("accessionNumber", []))
        filings = _compact_recent_filings(recent)
        filtered = []
        for filing in filings:
            if not _matches_form(filing.get("form"), form_types):
                continue
            if not _date_in_range(filing.get("filingDate"), start_date=start_date, end_date=end_date):
                continue
            filtered.append(filing)
            if len(filtered) >= size:
                break
        return {"cik": int(cik_str), "ticker": ticker, "filings": filtered, "total": len(filtered)}

    def dilution_filings(self, ticker=None, cik=None, start_date=None, end_date=None, size=100):
        return self.query_filings(ticker=ticker, cik=cik, form_types=DILUTION_FORMS, start_date=start_date, end_date=end_date, size=size)

    def important_events(self, ticker=None, cik=None, start_date=None, end_date=None, size=100):
        return self.query_filings(ticker=ticker, cik=cik, form_types=IMPORTANT_EVENT_FORMS, start_date=start_date, end_date=end_date, size=size)

    def insider_trading(self, ticker=None, cik=None, start_date=None, end_date=None, size=100):
        return self.query_filings(ticker=ticker, cik=cik, form_types=INSIDER_FORMS, start_date=start_date, end_date=end_date, size=size)

    def corporate_actions(self, ticker=None, cik=None, start_date=None, end_date=None, include_documents=False, size=100):
        submissions = self.submissions(ticker=ticker, cik=cik)
        action_filings = self.query_filings(
            cik=submissions.get("cik"),
            form_types=CORPORATE_ACTION_FORMS,
            start_date=start_date,
            end_date=end_date,
            size=size,
            require_us_exchange=False,
        )["filings"]
        result = {
            "cik": submissions.get("cik"),
            "name": submissions.get("name"),
            "tickers": submissions.get("tickers", []),
            "exchanges": submissions.get("exchanges", []),
            "formerNames": submissions.get("formerNames", []),
            "potentialCorporateActionFilings": action_filings,
        }
        if include_documents:
            patterns = ("reverse split", "stock split", "name change", "symbol change", "ticker symbol")
            matches = []
            for filing in action_filings:
                url = filing.get("reportUrl")
                if not url:
                    continue
                text = self.client.get_text(url)
                lowered = text.lower()
                if any(pattern in lowered for pattern in patterns):
                    filing_match = dict(filing)
                    filing_match["matchedTerms"] = [pattern for pattern in patterns if pattern in lowered]
                    matches.append(filing_match)
            result["matchedCorporateActionFilings"] = matches
        return result

    def filing_documents(self, cik, accession_no):
        url = filing_index_url(cik, accession_no)
        return self.client.get_json(url)

    def current_filings(self, form_type=None, count=100, owner="include"):
        params = {
            "action": "getcurrent",
            "owner": owner,
            "count": count,
            "output": "atom",
        }
        if form_type:
            params["type"] = form_type
        xml_text = self.client.get_text(SEC_CURRENT_ATOM_URL, params=params)
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = []
        allowed_ciks = None
        if self.allowed_exchanges:
            allowed_ciks = {int(row["cik"]) for row in self.get_company_tickers_exchange() if str(row.get("exchange", "")).upper() in self.allowed_exchanges}
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", default="", namespaces=ns)
            cik_text = _find_text_any(entry, "cik")
            if not cik_text:
                title_match = re.search(r"CIK[=: ]+(\d+)", title, flags=re.IGNORECASE)
                cik_text = title_match.group(1) if title_match else ""
            cik_value = int(cik_text) if cik_text.isdigit() else None
            if allowed_ciks is not None and cik_value not in allowed_ciks:
                continue
            link = entry.find("atom:link", ns)
            category = entry.find("atom:category", ns)
            entries.append(
                {
                    "title": title,
                    "updated": entry.findtext("atom:updated", default="", namespaces=ns),
                    "accessionNo": _find_text_any(entry, "accession-number"),
                    "form": _find_text_any(entry, "filing-type") or (category.get("term") if category is not None else ""),
                    "cik": cik_value,
                    "companyName": _find_text_any(entry, "company-name"),
                    "filingDetailsUrl": _find_text_any(entry, "filing-href") or (link.get("href") if link is not None else None),
                }
            )
        return entries

    def stream_filings(self, form_type=None, count=100, poll_interval=10, stop_after=None):
        seen = set()
        emitted = 0
        while True:
            for filing in reversed(self.current_filings(form_type=form_type, count=count)):
                key = filing.get("accessionNo") or (filing.get("cik"), filing.get("updated"), filing.get("title"))
                if key in seen:
                    continue
                seen.add(key)
                emitted += 1
                yield filing
                if stop_after and emitted >= stop_after:
                    return
            time.sleep(poll_interval)

    def daily_index_url(self, index_date=None, index_type="master"):
        value = index_date or date.today().isoformat()
        parsed = datetime.strptime(value, "%Y-%m-%d").date() if isinstance(value, str) else value
        quarter = (parsed.month - 1) // 3 + 1
        filename = "{}.{}{}.idx".format(index_type, parsed.strftime("%Y%m%d"), "")
        return SEC_WWW_BASE_URL + "/Archives/edgar/daily-index/{}/QTR{}/{}".format(parsed.year, quarter, filename)

    def download_daily_index(self, index_date=None, index_type="master"):
        return self.client.get_text(self.daily_index_url(index_date=index_date, index_type=index_type))
