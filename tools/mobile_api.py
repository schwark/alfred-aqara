"""Read-only research client. Runtime writes live in cloud_backend, separately."""

# Re-export the read-only client for existing research scripts and fixtures.
from cloud_transport import (ALLOWED, AppIdentity, CloudAPIError, CloudClient,
                              HOSTS, PREFIX, LOGIN, HOMES, DEVICES, DETAIL,
                              RESOURCES, PLUGINS, MATTER_DETAIL, MATTER_RESOURCES,
                              MAX_RESPONSE, NoRedirects, encrypt_password, signature,
                              secrets, time)
