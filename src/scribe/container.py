from dependency_injector import containers, providers

from scribe.config import Settings
from scribe.infrastructure.logging_config import configure_logging


class Container(containers.DeclarativeContainer):
    wiring_config = containers.WiringConfiguration(packages=["scribe.presentation"])

    settings = providers.Singleton(Settings)

    logging = providers.Resource(
        configure_logging,
        level=settings.provided.log_level,
        json_logs=settings.provided.log_json,
    )
