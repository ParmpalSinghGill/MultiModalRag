class RagctlError(Exception):
    """An expected failure with an operator-readable message."""


class LockHeld(RagctlError):
    pass


class WaitTimeout(RagctlError):
    pass


class DrainTimeout(RagctlError):
    pass


class SnapshotInvalid(RagctlError):
    pass


class UnsafePlan(RagctlError):
    pass


class TerraformFailed(RagctlError):
    pass


class VerificationFailed(RagctlError):
    pass
