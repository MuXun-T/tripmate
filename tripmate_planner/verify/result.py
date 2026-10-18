from dataclasses import dataclass, field


@dataclass
class VerifyResult:
    passed: bool
    commonsense_failed: list[str] = field(default_factory=list)
    commonsense_messages: list[str] = field(default_factory=list)
    spec_failed: list[int] = field(default_factory=list)

    @property
    def n_failures(self) -> int:
        return len(self.commonsense_failed) + len(self.spec_failed)
