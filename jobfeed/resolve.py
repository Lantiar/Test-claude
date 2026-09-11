"""Give a bare link a title and an employer.

A story carries a URL and nothing else. Simplify carries the company, the
title, the locations and the season, so a job that appears in both is complete
from the moment the two meet -- but a job only zero2sudo posted arrives as a
URL, and a list of five untitled links is not the thing that was asked for.

So the link is fetched once and read for whatever it says about itself, in
descending order of trust: a JSON-LD JobPosting block, which many ATSs emit and
which names the hiring organisation outright; then Open Graph; then the page
title, which usually reads "Job Title - Company Careers" and is parsed on that
assumption. What cannot be determined is left empty rather than guessed at: an
invented employer is worse than a blank one, because it would go on to match
against other jobs by that name.
"""
from __future__ import annotations

import json
import re
import urllib.request

from .identity import unwrap

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0.0.0 Safari/537.36")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:title|og:site_name)[\"'][^>]*"
    r"content=[\"'](.*?)[\"']", re.I)
_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
# "Software Engineer Intern - Acme Careers" / "SWE Intern | Acme"
_SPLIT = re.compile(r"\s+[|–—-]\s+")


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(unwrap(url), headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(400_000).decode("utf-8", "replace")


def describe(url: str, timeout: int = 20) -> dict:
    """{'title':…, 'company':…, 'locations':[…]} -- any key may be missing."""
    try:
        html = fetch(url, timeout)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:70]}"}

    out: dict = {}
    for blob in _LD.findall(html):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if "JobPosting" not in str(node.get("@type", "")):
                continue
            if t := node.get("title"):
                out["title"] = str(t).strip()
            org = node.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                out["company"] = str(org["name"]).strip()
            elif isinstance(org, str):
                out["company"] = org.strip()
            if loc := _places(node.get("jobLocation")):
                out["locations"] = loc
            if node.get("datePosted"):
                out["date_posted"] = str(node["datePosted"])
    if out.get("title") and out.get("company"):
        return out

    meta = {k.lower(): v for k, v in _META.findall(html)}
    if not out.get("company") and meta.get("og:site_name"):
        out["company"] = _company(_clean(meta["og:site_name"]))
    raw = meta.get("og:title") or (_TITLE.search(html).group(1) if _TITLE.search(html) else "")
    raw = _clean(raw)
    if raw and not out.get("title"):
        parts = [p.strip() for p in _SPLIT.split(raw) if p.strip()]
        out["title"] = parts[0] if parts else raw
        # The tail of a page title is the employer often enough to use and not
        # often enough to trust over anything else, so it is only taken when
        # nothing better said so.
        if len(parts) > 1 and not out.get("company"):
            out["company"] = _company(parts[-1])
    if not out.get("company"):
        out["company"] = company_from_url(url)
    return out


# The employer is in the path of every hosted job board, because that is how
# the board knows whose jobs to show. A page that does not name its own
# employer in JSON-LD or og tags -- careerpuck does not -- still cannot hide
# it from the URL that was used to reach it.
_SLUGS = (
    re.compile(r"//app\.careerpuck\.com/job-board/([^/?#]+)", re.I),
    re.compile(r"//(?:job-)?boards\.greenhouse\.io/(?!embed\b)([^/?#]+)", re.I),
    # The embedded application form carries it as a query parameter instead,
    # and those postings are the ones that arrived with no company at all --
    # or with a company scraped out of the page title, "US at DoorDash USA".
    re.compile(r"//(?:job-)?boards\.greenhouse\.io/embed/[^?#]*[?&]for=([^&#]+)", re.I),
    re.compile(r"//jobs\.lever\.co/([^/?#]+)", re.I),
    re.compile(r"//jobs\.ashbyhq\.com/([^/?#]+)", re.I),
    re.compile(r"//([^./]+)\.wd\d+\.myworkdayjobs\.com", re.I),
)


def company_from_url(url: str) -> str:
    """The employer's own slug out of a job board URL, or "".

    A last resort, used only when the page itself said nothing: Lyft's
    posting sat in the feed as company None with "lyft" in the middle of its
    address, and a job with no company is skipped by every later pass.
    """
    for pattern in _SLUGS:
        m = pattern.search(url or "")
        if not m:
            continue
        slug = m.group(1).strip("-_ ")
        # Slugs are lowercase and hyphenated; a name is neither. Single words
        # are safe to capitalise. Anything longer is left alone rather than
        # guessed into "Doordash Usa".
        words = [w for w in re.split(r"[-_]+", slug) if w]
        if len(words) == 1:
            return words[0].capitalize()
        return " ".join(words).strip()
    return ""


def _places(node) -> list[str]:
    places = []
    for n in (node if isinstance(node, list) else [node]):
        if not isinstance(n, dict):
            continue
        addr = n.get("address")
        for a in (addr if isinstance(addr, list) else [addr]):
            if isinstance(a, dict):
                bits = [a.get("addressLocality"), a.get("addressRegion")]
                if s := ", ".join(b for b in bits if b):
                    places.append(s)
    return places


def _company(s: str) -> str:
    """Trim what a page title or site name wraps the employer in.

    "Applied Data Solutions Program, Internships - Careers at Apple" yields
    "at Apple", and og:site_name gives "Amazon.jobs". Neither is the company's
    name, and both would be filed as separate employers from the Apple and
    Amazon rows Simplify already created.
    """
    s = re.sub(r"^\s*(at|@|join)\s+", "", (s or "").strip(), flags=re.I)
    s = re.sub(r"\b(careers?|jobs?|hiring|talent|recruiting)\b", " ", s, flags=re.I)
    s = re.sub(r"\.(jobs|com|io|co|net|org|ai)\b", " ", s, flags=re.I)
    return " ".join(s.split()).strip(" -|·,")


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&nbsp;", " ").replace("&#x27;", "'"))
    return " ".join(s.split())
