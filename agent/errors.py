class SSLConfigurationError(Exception):
    """Raised when SSL/TLS certificate bundle configuration fails."""
    pass


class ProviderRequestLimitReached(Exception):
    """Raised before dispatch when a request-limited agent has used its budget."""
