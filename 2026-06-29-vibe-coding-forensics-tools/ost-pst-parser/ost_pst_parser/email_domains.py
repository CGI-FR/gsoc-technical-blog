import re

from .local_config import (
    IGNORED_EMAIL_ADDRESSES,
    IGNORED_EMAIL_DOMAINS,
    INTERNAL_EMAIL_DOMAINS,
    PERSONAL_EMAIL_DOMAINS,
)


EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")


def s(value):
    """Return a safe string representation of a value."""
    if value is None:
        return ""
    return str(value)


def normalized_domain_set(domains):
    """Return normalized non-empty domain names."""
    return {s(domain).lower().strip() for domain in domains if s(domain).strip()}


def email_domains(value):
    """Return all email address domains found in a text value."""
    domains = []
    for address in EMAIL_RE.findall(s(value)):
        _local, _separator, domain = address.lower().rpartition("@")
        if domain:
            domains.append(domain)
    return domains


def email_addresses(value):
    """Return all email addresses found in a text value."""
    return [address.lower() for address in EMAIL_RE.findall(s(value))]


def domain_matches(domain, configured_domain):
    """Return whether a domain is exactly or hierarchically under another."""
    domain = domain.lower().strip()
    configured_domain = configured_domain.lower().strip()
    return domain == configured_domain or domain.endswith(f".{configured_domain}")


def is_ignored_email(address, ignored_addresses=None, ignored_domains=None):
    """Return whether an email address should be ignored by highlighting."""
    address = s(address).lower().strip()
    ignored_addresses = {s(item).lower().strip() for item in (ignored_addresses or IGNORED_EMAIL_ADDRESSES) if s(item).strip()}
    ignored_domains = normalized_domain_set(ignored_domains or IGNORED_EMAIL_DOMAINS)
    if address in ignored_addresses:
        return True
    _local, _separator, domain = address.rpartition("@")
    return any(domain_matches(domain, ignored_domain) for ignored_domain in ignored_domains)


def active_email_domains(value):
    """Return email domains after ignored addresses/domains are removed."""
    domains = []
    for address in email_addresses(value):
        if is_ignored_email(address):
            continue
        _local, _separator, domain = address.rpartition("@")
        if domain:
            domains.append(domain)
    return domains


def contains_external_email(value, internal_domains=None):
    """Return whether text contains an address outside configured internal domains."""
    internal_domains = normalized_domain_set(internal_domains or INTERNAL_EMAIL_DOMAINS)
    if not internal_domains:
        return False
    return any(
        not any(domain_matches(domain, internal_domain) for internal_domain in internal_domains)
        for domain in active_email_domains(value)
    )


def contains_personal_email(value, personal_domains=None):
    """Return whether text contains an address from a known personal provider."""
    personal_domains = normalized_domain_set(personal_domains or PERSONAL_EMAIL_DOMAINS)
    return any(domain in personal_domains for domain in active_email_domains(value))


def looks_like_email(value):
    """Return whether the entire value looks like a single email address."""
    return bool(EMAIL_RE.fullmatch(s(value).strip()))
