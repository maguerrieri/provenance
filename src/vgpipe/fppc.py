"""FPPC Form 700 search.

The portal at form700.fppc.ca.gov is a JS app, and `fppc.ca.gov/search-filings/
form-700-search/` is prose describing it — so a researcher with only an HTTP fetcher cannot
run the search a human runs. In testing that produced the exact failure this module exists
to prevent: a researcher cited an older year's Form 700, copied on another agency's site,
while the current filing sat at the top of the real search results.

The search endpoint the portal itself calls takes plain JSON and needs no session, so the
pipeline can ask the authoritative index directly which filing is newest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

SEARCH = "https://form700search.fppc.ca.gov/Home/SearchDocuments"
PDF_REQUEST = "https://form700search.fppc.ca.gov/Home/GetRedactedFormPdf"
PORTAL = "https://form700search.fppc.ca.gov/"


@dataclass
class Filing:
    filer: str
    filed_date: str
    filing_years: list[int]
    agencies: list[str]
    index_id: str
    is_amendment: bool = False
    positions: list[str] = field(default_factory=list)
    filing_type: str = ""

    @property
    def newest_year(self) -> int | None:
        return max(self.filing_years) if self.filing_years else None


def search(first: str, last: str, *, timeout: float = 45.0) -> list[Filing]:
    """Filings for a filer, newest first. Raises httpx errors to the caller."""
    body = {
        "queryGenerationInfo": None,
        "searchFieldQueryInfos": [
            {"queryField": "FilerFirstName", "queryType": "Start With", "filterValue": first},
            {"queryField": "FilerLastName", "queryType": "Start With", "filterValue": last},
        ],
        "showOnlyHeldPositions": False,
    }
    r = httpx.post(SEARCH, json=body, timeout=timeout, follow_redirects=True,
                   headers={"content-type": "application/json", "origin": PORTAL.rstrip("/"),
                            "referer": PORTAL, "user-agent": "Mozilla/5.0"})
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, str):      # the endpoint returns JSON-encoded JSON
        payload = json.loads(payload)
    return sorted(
        (_filing(d) for d in payload.get("documents", [])),
        key=lambda f: (f.filed_date or ""), reverse=True)


def citable_url(index_id: str, filing: "Filing | None" = None) -> str:
    """A stable URL for a Form 700 that the pipeline can re-fetch.

    **This URL is NOT fetchable, and nothing in the pipeline replays it.** An earlier version
    of this docstring claimed `fetch.py` recognised it and replayed the two-step API; it does
    not, and a researcher who believed that got `vg check` caching the JSON envelope and
    reporting the snippet absent. Measured again 2026-08-22: `GetRedactedFormPdf` returns a
    `PDFDownloadUrl` bound to the cookie jar that minted it, so a fresh client gets a 2,556
    byte error page, and every other document route returns the SPA shell.

    Consequence, stated plainly: **a Form 700 question is structurally `not_found`.** Use
    `vg form700` to establish which filing is current, then give the human a retrieval
    instruction (portal, name, filing year, agency). Do not cite a copy on another host to
    fill the gap — that verifies perfectly and is the wrong document.

    This URL is kept only as a stable identifier for *which* filing is meant.
    """
    from urllib.parse import urlencode

    q = {"indexID": index_id}
    if filing is not None:
        q.update({"last": filing.filer.split()[-1] if filing.filer else "",
                  "first": filing.filer.split()[0] if filing.filer else "",
                  "year": str(filing.newest_year or ""),
                  "agency": filing.agencies[0] if filing.agencies else "",
                  "position": filing.positions[0] if filing.positions else "",
                  "type": filing.filing_type or "Annual"})
    return f"{PDF_REQUEST}?{urlencode(q)}"


def document_pdf(index_id: str, *, last: str = "", first: str = "", year: str = "",
                 agency: str = "", position: str = "", filing_type: str = "Annual",
                 timeout: float = 120.0) -> bytes:
    """Fetch a filing's PDF. Two steps in one client. The download link works only for the
    cookie jar that minted it, so this can read a filing but yields nothing citable (see
    `citable_url`)."""
    body = {"indexID": index_id,
            "formInfo": {"LastName": last, "FirstName": first,
                         "FilingYear": int(year) if str(year).isdigit() else year,
                         "Agency": agency, "Position": position, "FilingType": filing_type}}
    headers = {"content-type": "application/json", "origin": PORTAL.rstrip("/"),
               "referer": PORTAL, "user-agent": "Mozilla/5.0"}
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"user-agent": "Mozilla/5.0"}) as c:
        r = c.post(PDF_REQUEST, json=body, headers=headers)
        r.raise_for_status()
        url = (r.json() or {}).get("PDFDownloadUrl")
        if not url:
            raise RuntimeError(f"no download url returned: {r.text[:200]}")
        pdf = c.get(url, headers={"referer": PORTAL})
        pdf.raise_for_status()
        if pdf.content[:4] != b"%PDF":
            raise RuntimeError(f"expected a PDF, got {pdf.headers.get('content-type')}")
        return pdf.content


def _filing(d: dict) -> Filing:
    info = d.get("filingInfo") or {}
    filer = d.get("filer") or {}
    positions = d.get("filingPositions") or []
    return Filing(
        filer=" ".join(x for x in (filer.get("firstName"), filer.get("lastName")) if x),
        filed_date=(info.get("filedDate") or "")[:10],
        filing_years=sorted({p["filingYear"] for p in positions if p.get("filingYear")}),
        agencies=sorted({p["agency"] for p in positions if p.get("agency")}),
        index_id=d.get("indexID", ""),
        is_amendment=bool(info.get("isAmendment")),
        positions=sorted({p["position"] for p in positions if p.get("position")}),
        filing_type=next((p["filingType"] for p in positions if p.get("filingType")), ""),
    )
