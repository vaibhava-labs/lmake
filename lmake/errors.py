class LMakeError(Exception):
    """Base class for expected lmake failures."""


class ConfigError(LMakeError):
    pass


class TargetError(LMakeError):
    pass


class RunNotFoundError(LMakeError):
    pass
