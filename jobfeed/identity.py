"""What makes two listings the same job.

Three kinds of evidence, in descending order of how much they are worth:

  Tier 1  the ATS's own identity for the posting -- (ats, org, posting_id)
          parsed out of the URL. Exact. Survives tracking parameters, mail
          redirects and shorteners, and two records that carry different
          Tier 1 identities are different jobs no matter how alike they read.

  Tier 2  the canonical URL: redirects followed, tracking parameters dropped,
          host lowercased. Catches the same posting shared two ways.

  Tier 3  the text -- company, title, location. Weak, and the only tier
          available for a source that never exposes the employer's own link.

Tier 1 does not cover everything and is not meant to. A survey of the 2181
live listings Simplify was carrying found 398 distinct hosts, of which the
five biggest are under a third of the corpus: every company with its own
careers domain is its own format. The families below are the ones with enough
listings behind them to be worth a pattern, and the long tail falls through to
Tier 2, which is what Tier 2 is for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse, urlunparse

# Query parameters that never change which posting a URL refers to. Stripped
# before a URL is used as an identity, so a link shared from an email campaign
# and the same link off the company's site are recognisably the same thing.
# gh_jid is deliberately absent: it is Greenhouse's job id, not tracking, and
# it is the only identifying thing in 82 of the live listings whose URL is an
# employer's own domain -- psiquantum.com/apply?gh_jid=7761881003. Stripping it
# with its lookalike neighbour gh_src (which really is a source tag) reduced
# every PsiQuantum posting to "psiquantum.com/" and every Tower Research one to
# "tower-research.com/open-positions", which is six jobs collapsed into two.
_JUNK_PARAMS = re.compile(
    r"^(utm_\w+|gh_src|src|source|ref|referer|referrer|fbclid|gclid|"
    r"mc_[ce]id|trk|trackingId|lever-source|campaign\w*|_ga|igshid|e)$", re.I)

# Instagram wraps every outbound story link in its own redirector, with the
# real destination sitting url-encoded in ?u= and a signature blob in ?e=.
# Unwrapped here rather than in the Instagram adapter because the wrapper says
# nothing about the posting and everything downstream -- the ATS patterns, the
# canonical form, the index-page guard -- would otherwise be reading
# l.instagram.com and finding a link shortener where there is a Greenhouse job.
_WRAPPERS = {
    "l.instagram.com": "u",
    "l.facebook.com": "u",
    "lm.facebook.com": "u",
    "away.vk.com": "to",
    "out.reddit.com": "url",
    "www.google.com": "url",          # /url?q= and /url?url=
}


def unwrap(url: str, hops: int = 3) -> str:
    """The real destination behind a redirector, without a network call."""
    for _ in range(hops):
        try:
            p = urlparse(url.strip())
        except ValueError:
            return url
        host = (p.netloc or "").lower()
        param = _WRAPPERS.get(host) or _WRAPPERS.get(host[4:] if host.startswith("www.") else "")
        if not param:
            return url
        q = parse_qs(p.query)
        target = (q.get(param) or q.get("q") or q.get("url") or [""])[0]
        if not target.startswith("http"):
            return url
        url = target
    return url


# ATSs whose posting id is unique across the whole product, not just within
# one employer's account. For these the employer is metadata and must be left
# out of the identity, because the same posting legitimately appears under two
# different orgs: Greenhouse serves one job both as
# boards.greenhouse.io/point72/jobs/8389431002 and, embedded in the company's
# own careers page, as boards.greenhouse.io/embed/job_app?token=8389431002 --
# same posting, same id, and no employer in the second URL at all. Keying on
# the employer there would file one job twice.
#
# The others are the opposite case and must keep it. A Workday requisition
# number is unique inside a tenant and nowhere else: R-12345 at RBC and R-12345
# at Disney are two unrelated jobs, and merging them would silently lose one.
_GLOBAL_ID = {"greenhouse", "lever", "ashby", "smartrecruiters",
              "tiktok", "bytedance", "tesla", "apple", "amazon"}


@dataclass(frozen=True)
class Identity:
    """The ATS's own name for a posting."""
    ats: str
    org: str
    posting_id: str

    @property
    def key(self) -> str:
        org = "" if self.ats in _GLOBAL_ID else self.org
        return f"{self.ats}:{org}:{self.posting_id}".lower()


# (ats, host pattern, path pattern). Order matters only in that the first
# match wins; the patterns are disjoint in practice.
_PATTERNS: tuple[tuple[str, re.Pattern, re.Pattern], ...] = (
    # boards.greenhouse.io/acme/jobs/123  and job-boards.greenhouse.io/...
    ("greenhouse", re.compile(r"(^|\.)(job-boards|boards)\.greenhouse\.io$", re.I),
     re.compile(r"^/([^/]+)/jobs/(\d+)", re.I)),
    # amazon.jobs/en/jobs/10517567/software-development-engineer-intern...
    # and amazon.jobs/en/jobs/10502743 with no slug at all: same job number.
    ("amazon", re.compile(r"(^|\.)amazon\.jobs$", re.I),
     re.compile(r"^/[^/]+/(jobs)/(\d{5,})", re.I)),
    # lifeattiktok.com/search/7572665884037826869
    ("tiktok", re.compile(r"(^|\.)lifeattiktok\.com$", re.I),
     re.compile(r"^/(search)/(\d{6,})", re.I)),
    # jobs.bytedance.com/en/position/7595676762475415861/detail. Kept apart
    # from TikTok deliberately: both carry 19-digit ids of the same shape and
    # they may well be one namespace, but "may well be" is not evidence, and
    # being wrong here merges two unrelated jobs into one. Being wrong the
    # other way shows a duplicate, which is the failure worth having.
    ("bytedance", re.compile(r"(^|\.)jobs\.bytedance\.com$", re.I),
     re.compile(r"^/[^/]+/(position)/(\d{6,})", re.I)),
    # jobs.apple.com/en-us/details/200673612-0836/applied-data-solutions...
    # The number before the dash is the posting; the suffix varies between
    # links to one job -- Simplify carries 200673612 and the same job shared
    # in a story carries 200673612-0836.
    ("apple", re.compile(r"(^|\.)jobs\.apple\.com$", re.I),
     re.compile(r"^/[^/]+/(details)/(\d{6,})", re.I)),
    # tesla.com/careers/search/job/269819
    ("tesla", re.compile(r"(^|\.)tesla\.com$", re.I),
     re.compile(r"^/careers/[^/]+/(job)/(\d+)", re.I)),
    # jobs.lever.co/acme/<uuid>[/apply]
    ("lever", re.compile(r"(^|\.)jobs\.lever\.co$", re.I),
     re.compile(r"^/([^/]+)/([0-9a-f-]{36})", re.I)),
    # jobs.ashbyhq.com/acme/<uuid>[/application]
    ("ashby", re.compile(r"(^|\.)jobs\.ashbyhq\.com$", re.I),
     re.compile(r"^/([^/]+)/([0-9a-f-]{36})", re.I)),
    # jobs.smartrecruiters.com/Acme/744000107100902
    ("smartrecruiters", re.compile(r"(^|\.)jobs\.smartrecruiters\.com$", re.I),
     re.compile(r"^/([^/]+)/(\d+)", re.I)),
    # apply.workable.com/acme/j/DD49E21C54/
    ("workable", re.compile(r"(^|\.)workable\.com$", re.I),
     re.compile(r"^/([^/]+)/j/([0-9A-F]+)", re.I)),
    # ats.rippling.com/acme/jobs/<uuid>
    ("rippling", re.compile(r"(^|\.)rippling\.com$", re.I),
     re.compile(r"^/([^/]+)/jobs?/([0-9a-f-]{36})", re.I)),
)

# Workday is a host pattern rather than a fixed host: every tenant gets its own
# subdomain (rbc.wd3, psu.wd1, nvidia.wd5...), which is why it looks like a long
# tail in a host histogram and is really one family with 58 tenants in it.
_WORKDAY_HOST = re.compile(r"^([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.I)
_WORKDAY_REQ = re.compile(r"(R-?\d[\w-]*|JR-?\d[\w-]*)", re.I)

# iCIMS: careers-acme.icims.com/jobs/12345/...
_ICIMS_HOST = re.compile(r"^([\w-]+)\.icims\.com$", re.I)
_ICIMS_PATH = re.compile(r"/jobs/(\d+)", re.I)

# Oracle Recruiting, one tenant per subdomain like Workday:
# egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011984
_ORACLE_HOST = re.compile(r"^([\w-]+)\.fa\.[\w-]+\.oraclecloud\.com$", re.I)
_ORACLE_PATH = re.compile(r"/job/(\d+)", re.I)

# Greenhouse embedded in an employer's own careers page. The posting id lives
# in the query string and the employer is absent, which is why it needs its own
# branch rather than a path pattern -- and why greenhouse ids must not be keyed
# on the employer. 25 live listings arrive in this shape.
_GH_EMBED_HOST = re.compile(r"(^|\.)(job-boards|boards)\.greenhouse\.io$", re.I)


def canonical_url(url: str) -> str:
    """The same link with everything that does not name the posting removed."""
    url = unwrap(url)
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    if not p.scheme or not p.netloc:
        return url.strip()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    kept = {k: v for k, v in parse_qs(p.query, keep_blank_values=False).items()
            if not _JUNK_PARAMS.match(k)}
    query = "&".join(f"{k}={v[0]}" for k, v in sorted(kept.items()))
    path = p.path.rstrip("/") or "/"
    # Trailing application steps are the same posting as the posting itself.
    path = re.sub(r"/(apply|application|apply/?form)$", "", path, flags=re.I) or "/"
    return urlunparse(("https", host, path, "", query, ""))


def identify(url: str) -> Identity | None:
    """The ATS's identity for this posting, when the URL carries one."""
    url = unwrap(url)
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    host, path = p.netloc.lower(), p.path
    if not host:
        return None

    for ats, host_re, path_re in _PATTERNS:
        if host_re.search(host) and (m := path_re.search(path)):
            return Identity(ats, m.group(1).lower(), m.group(2).lower())

    if m := _WORKDAY_HOST.match(host):
        # The requisition number is the identity; the rest of a Workday path is
        # the tenant's own site structure and varies between links to one job.
        if req := _WORKDAY_REQ.search(path):
            return Identity("workday", m.group(1).lower(), req.group(1).upper())
        # No requisition in the path: fall back to the last path segment, which
        # is the job slug, rather than claiming no identity at all.
        if tail := [s for s in path.split("/") if s]:
            return Identity("workday", m.group(1).lower(), tail[-1].lower())

    if (m := _ICIMS_HOST.match(host)) and (j := _ICIMS_PATH.search(path)):
        return Identity("icims", m.group(1).lower(), j.group(1))

    if (m := _ORACLE_HOST.match(host)) and (j := _ORACLE_PATH.search(path)):
        return Identity("oracle", m.group(1).lower(), j.group(1))

    if _GH_EMBED_HOST.search(host) and path.rstrip("/").endswith("/embed/job_app"):
        token = parse_qs(p.query).get("token", [""])[0]
        if token.isdigit():
            return Identity("greenhouse", "", token)

    # A Greenhouse board embedded in the employer's own site, on the employer's
    # own domain: psiquantum.com/apply?gh_jid=7761881003,
    # x.company/careers/8425736002?gh_jid=8425736002. The host says nothing --
    # it is whatever the company owns -- but gh_jid is Greenhouse's id for the
    # posting and is the same number the board would serve it under. Worth
    # having on any host: it promotes 82 of the live listings out of the
    # untyped tail and into an exact identity.
    if jid := parse_qs(p.query).get("gh_jid", [""])[0]:
        if jid.isdigit():
            return Identity("greenhouse", "", jid)

    return None


# A path segment that could be a posting's own id: a number, a uuid, a hash, or
# a slug long enough to be a job title rather than a section of a website.
_ID_SEGMENT = re.compile(
    r"^(?:\d{4,}"                        # 269819, 8389431002
    r"|[0-9a-f]{8}-[0-9a-f-]{20,}"       # uuid
    r"|[0-9a-f]{16,}"                    # hex blob
    r"|[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*-[A-Za-z0-9_-]{6,}"   # slug-with-id
    r"|[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+){3,})$")            # long-enough slug


def looks_like_one_posting(url: str) -> bool:
    """Does this URL name a single job, or a page that lists many?

    Asked because a canonical URL is otherwise trusted as an identity, and for
    a good fraction of employers the link on offer is the careers index rather
    than the posting. Fifteen live Zipline listings -- Hardware Test Intern,
    Perception Intern, Computational Physics Intern, each a real and separate
    job -- all carry the URL zipline.com/open-roles, and so do four Tower
    Research listings and three at Hudson River Trading. Treating that URL as
    an identity does not produce a near-miss or a bad ranking: it files fifteen
    jobs as one and loses fourteen of them, and the loss is invisible, because
    what is left looks exactly like a correctly deduplicated row.

    So the URL has to carry something that could name a posting. Nothing here
    guarantees a link is a posting -- only that a link with no identifying
    segment anywhere in it is not trustworthy as one, which is the direction
    that matters. Anything rejected falls through to being matched on its
    company, title and location instead, which keeps the fifteen apart.
    """
    # Judged on the canonical form, because that is the string that would
    # become the key. Reading the raw URL instead let four PsiQuantum and Tower
    # Research listings through: raw, they carry ?gh_jid=8024138 and look
    # perfectly well identified; canonical, the id had been stripped and all
    # four were the bare careers page. A guard has to inspect the thing it is
    # guarding, not an earlier draft of it.
    url = canonical_url(url)
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    if not p.netloc:
        return False
    segments = [s for s in p.path.split("/") if s]
    if any(_ID_SEGMENT.match(s) for s in segments):
        return True
    # An id in the query string counts too: ?token=7231006, ?jobId=1234.
    for values in parse_qs(p.query).values():
        if any(v.isdigit() and len(v) >= 4 for v in values):
            return True
        if any(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{20,}", v, re.I) for v in values):
            return True
    return False
