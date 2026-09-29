import os

from fastapi import FastAPI
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

app = FastAPI(
    title="Tech Adapter Micro Service",
    description="Microservice responsible to handle provisioning and access control requests for one or more data product components.",  # noqa: E501
    version="2.2.0",
)

# Path to the mounted, non-secret configuration file (see
# helm/files/application.yaml, merged by helm/templates/configmap.yaml and
# mounted at this same relative path under the container's /app working
# directory, see helm/templates/deployment.yaml). Overridable for local runs
# outside the container.
CONFIG_FILE_PATH = os.environ.get("TECH_ADAPTER_CONFIG_FILE", "config/application.yaml")


class SodaConfig(BaseSettings):
    """
    Soda Cloud API connectivity configuration.

    Every field is named after, and can be set by, an environment variable
    with the `SODA_` prefix (e.g. `base_url` <-> `SODA_BASE_URL`). Non-secret
    fields also fall back to the mounted `application.yaml` file (see
    helm/files/application.yaml) using the same field name as the YAML key
    (e.g. `base_url`). Precedence, highest first: explicit constructor
    argument, environment variable, mounted YAML file, then the default
    below.

    `api_key_id` and `api_key_secret` are always injected as environment
    variables from a Kubernetes Secret (see docs/technical-details.md) and
    must never appear in `application.yaml` or the component descriptor.
    """

    model_config = SettingsConfigDict(env_prefix="SODA_", extra="ignore")

    api_key_id: str | None = None
    api_key_secret: str | None = None
    base_url: str = "https://cloud.soda.io"
    request_timeout_seconds: float = 30.0
    polling_interval_seconds: float = 5.0
    polling_max_timeout_seconds: float = 300.0
    polling_backoff_multiplier: float = 1.5
    polling_backoff_max_interval_seconds: float = 30.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, [CONFIG_FILE_PATH]),
        )

    def is_configured(self) -> bool:
        return bool(self.api_key_id and self.api_key_secret)


soda_config = SodaConfig()
