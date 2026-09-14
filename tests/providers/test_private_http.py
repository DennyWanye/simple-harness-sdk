"""Explicit local deployment support retains default public transport refusal."""

import httpx
import pytest

from simple_harness.providers import OpenAICompatibleProvider, Secret


@pytest.mark.parametrize(
    "url",
    [
        "http://10.1.2.3/v1",
        "http://192.168.10.27:11434/v1",
        "http://172.16.0.1/v1",
        "http://[fd00::1]/v1",
    ],
)
def test_private_literal_requires_explicit_option(url):
    client = httpx.AsyncClient()
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleProvider(client, url, "local", Secret("fixture"))
    provider = OpenAICompatibleProvider(
        client, url, "local", Secret("fixture"), allow_private_http=True
    )
    assert provider.target.endpoint_identity.startswith(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com/v1",
        "http://8.8.8.8/v1",
        "http://169.254.169.254/v1",
        "http://example.invalid/v1",
        "http://0.0.0.0/v1",
    ],
)
def test_public_metadata_unspecified_and_dns_http_still_refused(url):
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleProvider(
            httpx.AsyncClient(), url, "local", Secret("fixture"), allow_private_http=True
        )


def test_private_option_is_not_truthy_coerced():
    with pytest.raises(TypeError, match="boolean"):
        OpenAICompatibleProvider(
            httpx.AsyncClient(),
            "http://192.168.1.2/v1",
            "local",
            Secret("fixture"),
            allow_private_http="yes",
        )
