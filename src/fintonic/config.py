"""Static configuration reverse-engineered from the Fintonic web app (www.fintonic.com/private)."""

API_BASE = "https://api.fintonic.com/finapi"
BFF_BASE = "https://fintonic-pfm-bff.fintonic.com"

# The API is versioned through the Accept media type; the web app pins v7.
API_VERSION = "7"
ACCEPT = f"application/vnd.fintonic-v{API_VERSION}+json"

# Cloudflare sits in front of the API, so present ourselves like the web app does.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "accept": ACCEPT,
    "psd2_support": "true",
    "user-agent": USER_AGENT,
    "origin": "https://www.fintonic.com",
    "referer": "https://www.fintonic.com/",
}

KEYRING_SERVICE = "fintonic-cli"

# Seconds before JWT expiry at which we consider the access token stale.
TOKEN_EXPIRY_SKEW = 60
