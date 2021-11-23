from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from gretel_client.projects.jobs import Job
else:
    Job = None


# Projects


class GretelProjectError(Exception):
    """Problems with a Gretel Project."""

    ...


# Jobs, Models, Record Handlers


class GretelResourceNotFound(Exception):
    """Baseclass for errors locating remote Gretel resources.

    The ``context`` property may be overridden to provide additional
    information that may be helpful in correctly addressing the
    Gretel resource. Generally, the context should follow a simple
    key, value dictionary pattern.
    """

    @property
    def context(self) -> Dict[str, Any]:
        return {}


class GretelJobNotFound(GretelResourceNotFound):
    """The job could not be found. May be either a model
    or record handler."""

    def __init__(self, job: Job):
        self._job = job
        super().__init__()

    def __str__(self) -> str:
        return f"The {self._job.job_type} '{self._job.id}' could not be found"

    @property
    def context(self) -> Dict[str, Optional[str]]:
        return {
            f"{self._job.job_type}_id": self._job.id,
            "project_id": self._job.project.project_id,
        }


class RecordHandlerNotFound(GretelJobNotFound):
    """Failed to lookup the remote record handler."""

    ...


class ModelNotFoundError(GretelJobNotFound):
    """Failed to lookup the remote model."""

    ...


# Model Config


class ModelConfigError(Exception):
    """Could not read or load the specified model configuration."""

    ...


# Data Sources


class DataSourceError(Exception):
    """Indicates there is a problem reading the data source."""

    ...


class DataValidationError(Exception):
    """Indicates there is a problem validating the structure
    of the data source.
    """

    ...


# Run Errors


class ContainerRunError(Exception):
    """There was a problem running the job from a local container."""

    ...


class DockerEnvironmentError(Exception):
    """The host docker environment isn't configured correctly."""

    ...


class WaitTimeExceeded(Exception):
    """
    Thrown when the wait time specified by the user has expired.
    """

    ...


class ModelError(Exception):
    """There was a problem creating the model."""

    ...


class RecordHandlerError(Exception):
    """There was a problem run the model."""

    ...
