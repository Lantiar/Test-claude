"""What counts as the same posting. Every case here came out of live data."""
from jobfeed.identity import Identity, canonical_url, identify, looks_like_one_posting


def test_greenhouse_board_and_embed_are_one_posting():
    # Same job, two ways Greenhouse serves it, and the embed carries no
    # employer at all -- so the employer must not be part of the key.
    a = identify("https://boards.greenhouse.io/point72/jobs/8389431002")
    b = identify("https://boards.greenhouse.io/embed/job_app?token=8389431002")
    assert a and b and a.key == b.key


def test_gh_jid_on_an_employers_own_domain():
    i = identify("https://www.psiquantum.com/apply?gh_jid=7761881003")
    assert i == Identity("greenhouse", "", "7761881003")


def test_gh_jid_survives_canonicalisation():
    # It is an id, not tracking: stripping it reduced every PsiQuantum posting
    # to the same bare URL.
    assert "gh_jid=7761881003" in canonical_url(
        "https://www.psiquantum.com/apply?gh_jid=7761881003&utm_source=x")


def test_workday_requisitions_are_scoped_to_their_tenant():
    # R-12345 means nothing outside the tenant that issued it.
    a = identify("https://rbc.wd3.myworkdayjobs.com/en-US/x/job/Toronto/R-12345")
    b = identify("https://disney.wd5.myworkdayjobs.com/en-US/y/job/LA/R-12345")
    assert a and b and a.key != b.key


def test_tracking_parameters_do_not_change_identity():
    one = canonical_url("https://jobs.lever.co/acme/2f1c261d-9b65-412b-9f17-34b8968bdd78/apply?utm_source=x")
    two = canonical_url("https://jobs.lever.co/acme/2f1c261d-9b65-412b-9f17-34b8968bdd78")
    assert one == two


def test_a_careers_index_is_not_a_posting():
    # Fifteen live Zipline internships shared this URL.
    assert not looks_like_one_posting("https://zipline.com/open-roles")
    assert not looks_like_one_posting("https://hudsonrivertrading.com/careers/job")
    assert looks_like_one_posting("https://jobs.apple.com/en-us/details/200663981")


def test_the_guard_judges_the_canonical_form():
    # Raw, this carries an id; canonical, the id is kept -- but a URL whose
    # only id is a parameter we strip must be rejected, not trusted.
    assert not looks_like_one_posting("https://tower-research.com/open-positions/?utm_campaign=1")
