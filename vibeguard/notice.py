"""Notice injected into model context when masking is active."""


def build_placeholder_notice(prefix: str = "__VG_") -> str:
    return (
        "[VibeGuard] Sensitive values in this conversation were masked locally before reaching you. "
        "Site and device names appear as stable codes such as __VG_HIDDEN_S12__ and __VG_HIDDEN_K3__; "
        "ID numbers, phone numbers and credentials appear as realistic-looking random values. "
        "These are intentional references, NOT file corruption, encoding errors, or mistakes: do not "
        "decode, reconstruct, or ask the user to confirm them. If you must repeat one (for example "
        "inside a shell command, path, or code edit), reproduce it verbatim; local tooling resolves it "
        "to the real value before execution. Never invent the value behind a code."
    )
