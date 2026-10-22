from types import SimpleNamespace

from tools.browser.base.driver import Browser


class FakeRoute:
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.request = SimpleNamespace(url=url, headers=headers or {})
        self.continued_with = None

    def continue_(self, **kwargs):
        self.continued_with = kwargs


def test_vercel_bypass_header_only_goes_to_exact_allowlisted_host(tmp_path):
    browser = Browser(
        tmp_path,
        vercel_bypass_secret="secret-value",
        vercel_bypass_hosts=("app-dev.example.com",),
    )

    allowed = FakeRoute("https://app-dev.example.com/zh", {"accept": "text/html"})
    browser._route_with_vercel_bypass(allowed)
    assert allowed.continued_with["headers"]["x-vercel-protection-bypass"] == "secret-value"
    assert allowed.continued_with["headers"]["accept"] == "text/html"

    subdomain = FakeRoute("https://evil.app-dev.example.com/collect")
    browser._route_with_vercel_bypass(subdomain)
    assert subdomain.continued_with == {}

    third_party = FakeRoute("https://vercel.com/login")
    browser._route_with_vercel_bypass(third_party)
    assert third_party.continued_with == {}
