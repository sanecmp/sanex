"""Application-agnostic recognition pipeline and conservative process grouping."""

from dataclasses import dataclass, field

from ..model.process import ProcessIdentity
from .common import ApplicationContext, ApplicationMatch, ApplicationProvider, MetadataReader


@dataclass(frozen=True, slots=True)
class ApplicationResolver:
    """Resolve applications without processes, network requests or a procfs scan."""

    metadata: MetadataReader = field(default_factory=MetadataReader)
    providers: tuple[ApplicationProvider, ...] = ()
    environment_keys: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        keys = frozenset().union(*(provider.environment_keys for provider in self.providers))
        object.__setattr__(self, "environment_keys", keys)

    def identify(self, process: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch:
        """Identify once per snapshot, retaining raw process identities unchanged."""

        if process not in context.matches:
            match = None

            for provider in self.providers:

                if (match := provider.identify(process, context)) is not None:
                    break

            if match is None:
                match = ApplicationMatch(process)

            context.matches[process] = match

        return context.matches[process]

    def resolve(self, process: ProcessIdentity, context: ApplicationContext) -> ApplicationMatch:
        """Group generic workers only when executable and semantic identity agree."""
        match = self.identify(process, context)

        if not match.group_same_executable:
            return match

        root = match.process

        for parent in context.walk_parents(root):

            if parent.exe != root.exe:
                break

            parent_match = self.identify(parent, context)

            if not parent_match.group_same_executable or parent_match.app_id != match.app_id:
                break

            root = parent_match.process

        return ApplicationMatch(root, match.application)
