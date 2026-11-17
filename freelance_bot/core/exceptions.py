"""Known operational failures used for safe account pausing."""


class SourceError(Exception):
    """Base class for source adapter failures."""


class AuthenticationRequired(SourceError):
    """The account needs a manual login or session refresh."""


class CaptchaDetected(SourceError):
    """A human verification step was detected."""


class PlatformChanged(SourceError):
    """The source page changed and automation must stop safely."""
