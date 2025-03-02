import json

from copy import deepcopy
from typing import Dict, List, Optional, Tuple, Union

import click

from gretel_client.cli.common import (
    get_model,
    model_option,
    pass_session,
    poll_and_print,
    project_option,
    record_handler_option,
    ref_data_option,
    runner_option,
    SessionContext,
    StatusDescriptions,
)
from gretel_client.cli.models import models
from gretel_client.cli.utils.parser_utils import ref_data_factory, RefData
from gretel_client.config import RunnerMode
from gretel_client.models.config import get_model_type_config
from gretel_client.projects.docker import ContainerRun
from gretel_client.projects.jobs import Status
from gretel_client.projects.records import RecordHandler

LOCAL = "__local__"


@click.group(help="Commands for working with records and running models.")
def records():
    ...


def model_path_option(fn):
    return click.option(
        "--model-path",
        metavar="PATH",
        help="Specify a remote path to the model.",
    )(fn)


def input_data_option(fn):
    def callback(ctx, param: click.Option, value: str):
        gc: SessionContext = ctx.ensure_object(SessionContext)
        return value or gc.data_source

    return click.option(  # type: ignore
        "--in-data",
        metavar="PATH",
        callback=callback,
        help="Specify the model input data.",
    )(fn)


def output_data_option(fn):
    return click.option(
        "--output",
        metavar="DIR",
        help="Specify the model output directory.",
    )(fn)


def _validate_params(
    sc: SessionContext, runner: str, output: str, model_path: str, in_data: str
):
    if runner == RunnerMode.CLOUD.value and model_path:
        raise click.BadOptionUsage(
            "--model-path", "A model path may not be specified for cloud models."
        )
    if (
        not sc.model.is_cloud_model
        and runner == RunnerMode.LOCAL.value
        and not model_path
    ):
        raise click.BadOptionUsage(
            "--model-path", "--model-path is required when running a local model."
        )
    if runner == RunnerMode.LOCAL.value and not output:
        raise click.BadOptionUsage(
            "--output",
            "--runner is set to local, but no --output flag is set. Please set an output path.",
        )

    if runner == RunnerMode.LOCAL.value and sc.model.is_cloud_model and model_path:
        raise click.BadOptionUsage(
            "--model-path", "Cannot specify the local model path for cloud models."
        )

    if runner == RunnerMode.MANUAL.value and (output or model_path):
        raise click.BadOptionUsage(
            "--runner",
            "--runner manual cannot be used together with any of --output, --model-path.",
        )


def _check_model_and_runner(sc: SessionContext, runner) -> str:
    if not sc.model.is_cloud_model and runner is RunnerMode.CLOUD.value:
        sc.log.info(
            (
                f"The model {sc.model.model_id} is configured for local runs, "
                f"but runner_mode is {runner}."
            )
        )
        sc.log.info("Setting runner_mode to local and running the model.")
        return RunnerMode.LOCAL.value
    return runner


def _configure_data_source(
    sc: SessionContext, in_data: Optional[str], runner: str
) -> Optional[str]:
    # NOTE: If ``in_data`` is already None, we just return None which
    # will then be passed into the record handler API call. If the job
    # being requested does require a data source, then the API will reject
    # the API call. If the data source is optional, then the API call will succeed.
    #
    # If the job is local then the "__local__" string will be passed into the API
    # call and stored in the cloud config. This is pro forma in order to get the record
    # handler config validators to pass. When the job container actually starts, this
    # __local__ value will be replaced by the actual data source

    if in_data is None:
        return None

    if runner == RunnerMode.MANUAL.value:
        data_source = in_data
    elif runner == RunnerMode.CLOUD.value:
        sc.log.info(f"Uploading input artifact {in_data}")
        data_source = sc.project.upload_artifact(in_data)
    else:
        data_source = LOCAL

    return data_source


def _configure_ref_data(sc: SessionContext, ref_data: RefData, runner: str) -> RefData:
    if not ref_data.is_empty and runner == RunnerMode.MANUAL.value:
        return ref_data
    if not ref_data.is_empty and runner == RunnerMode.CLOUD.value:
        sc.log.info("Uploading ref data...")
        ref_dict = {}
        for key, data_source in ref_data.ref_dict.items():
            data_source = sc.project.upload_artifact(data_source)
            ref_dict[key] = data_source
        ref_data = ref_data_factory(ref_dict)
    else:
        # If the job is being run locally, we swap the data sources to __local__
        # so we don't expose anything in the stored cloud config
        for key, data_source in ref_data.ref_dict.items():
            ref_data.ref_dict[key] = LOCAL
    return ref_data


def create_and_run_record_handler(
    sc: SessionContext,
    *,
    params: Optional[dict],
    action: Optional[str],
    runner: str,
    output: str,
    in_data: Optional[str],
    model_path: Optional[str],
    data_source: Optional[str],
    status_strings: StatusDescriptions,
    ref_data: Optional[RefData] = None,
    config_ref_data: Optional[RefData] = None,
):

    # NOTE: ``in_data`` is only evaluated to determine if we should set a --data-source
    # for the CLI args that get passed into a local container.  The ``data_source`` value
    # is what will be sent to the Cloud API.

    sc.log.info(f"Creating record handler for model {sc.model.model_id}.")
    record_handler = sc.model.create_record_handler_obj()

    if config_ref_data is None:
        config_ref_data = ref_data_factory()

    data = record_handler._submit(
        params=params,
        action=action,
        runner_mode=RunnerMode(runner),
        data_source=data_source,
        ref_data=config_ref_data,
        _default_manual=True,
    )
    sc.register_cleanup(lambda: record_handler.cancel())
    sc.log.info(f"Record handler created {record_handler.record_id}.")

    printable_record_handler = data.print_obj
    if runner == RunnerMode.MANUAL.value:
        # With --runner MANUAL, we only print the worker_key and it's up to the user to run the worker
        sc.print(
            data={
                "record_handler": printable_record_handler,
                "worker_key": record_handler.worker_key,
            }
        )
    else:
        sc.print(data=printable_record_handler)

    run = None
    if runner == RunnerMode.LOCAL.value:
        run = ContainerRun.from_job(record_handler)
        if sc.debug:
            sc.log.debug("Enabling debug for the container run.")
            run.enable_debug()
        if output:
            run.configure_output_dir(output)
        if in_data:
            run.configure_input_data(in_data)
        if model_path:
            run.configure_model(model_path)
        if ref_data:
            run.configure_ref_data(ref_data)
        run.start()
        sc.register_cleanup(lambda: run.graceful_shutdown())

    # Poll for the latest container status
    poll_and_print(
        record_handler, sc, runner, status_strings, callback=run.is_ok if run else None
    )

    if output and runner == RunnerMode.CLOUD.value:
        record_handler.download_artifacts(output)

    if output and run:
        sc.log.info("Extracting record artifacts from the container.")
        run.extract_output_dir(output)

    if record_handler.status == Status.COMPLETED:
        sc.log.info(
            (
                "For a more detailed view, you can download record artifacts using the CLI command \n\n"
                f"\tgretel records get --project {sc.project.name} --model-id {sc.model.model_id} --record-handler-id {record_handler.record_id} --output .\n"
            )
        )
        sc.log.info(
            (
                "Billing estimate"
                f"\n{json.dumps(record_handler.billing_details, indent=4)}."
            )
        )
        sc.log.info("Done.")
    else:
        sc.log.error("The record command failed with the following error.")
        sc.log.error(record_handler.errors)
        sc.log.error(
            f"Status is {record_handler.status}. Please scroll back through the logs for more details."
        )
        sc.exit(1)


@records.command(help="Generate synthetic records from a model.")
@project_option
@runner_option
@model_option
@model_path_option
@output_data_option
@input_data_option
@click.option("--num-records", help="Number of records to generate.", default=500)
@click.option(
    "--max-invalid",
    help="Number of invalid records generated before failure.",
    default=None,
)
@pass_session
def generate(
    sc: SessionContext,
    project: str,
    runner: str,
    output: str,
    in_data: str,
    model_path: str,
    model_id: str,
    num_records: int,
    max_invalid: int,
):
    runner = _check_model_and_runner(sc, runner)
    _validate_params(sc, runner, output, model_path, in_data)

    data_source = _configure_data_source(sc, in_data, runner)

    params: Dict[str, Union[int, float, str]] = {
        "num_records": num_records,
        "max_invalid": max_invalid,
    }

    create_and_run_record_handler(
        sc,
        params=params,
        data_source=data_source,
        action="generate",
        runner=runner,
        output=output,
        in_data=in_data,
        status_strings=get_model_type_config("synthetics").run_status_descriptions,
        model_path=model_path,
    )


@records.command(help="Transform records via pipelines.")
@project_option
@runner_option
@model_option
@model_path_option
@input_data_option
@output_data_option
@pass_session
def transform(
    sc: SessionContext,
    project: str,
    model_path: str,
    in_data: str,
    output: str,
    runner: str,
    model_id: str,
):
    runner = _check_model_and_runner(sc, runner)
    _validate_params(sc, runner, output, model_path, in_data)

    data_source = _configure_data_source(sc, in_data, runner)

    create_and_run_record_handler(
        sc,
        params=None,
        data_source=data_source,
        action="transform",
        runner=runner,
        output=output,
        in_data=in_data,
        status_strings=get_model_type_config("transform").run_status_descriptions,
        model_path=model_path,
    )


@records.command(help="Classify records.")
@project_option
@runner_option
@model_path_option
@input_data_option
@output_data_option
@model_option
@pass_session
def classify(
    sc: SessionContext,
    project: str,
    in_data: str,
    output: str,
    runner: str,
    model_id: str,
    model_path: str,
):
    runner = _check_model_and_runner(sc, runner)
    _validate_params(sc, runner, output, model_path, in_data)

    data_source = _configure_data_source(sc, in_data, runner)

    create_and_run_record_handler(
        sc,
        params=None,
        data_source=data_source,
        action="classify",
        runner=runner,
        output=output,
        in_data=in_data,
        status_strings=get_model_type_config("classify").run_status_descriptions,
        model_path=model_path,
    )


def action_option(fn):
    return click.option("--action", help="Specify action to run.", type=str)(fn)


@models.command(help="Run an existing model.")
@project_option
@runner_option
@model_path_option
@input_data_option
@ref_data_option
@output_data_option
@model_option
@pass_session
@action_option
@click.option(
    "--param",
    type=(str, str),
    multiple=True,
    help="Specify parameters to pass into the record handler.",
)
def run(
    sc: SessionContext,
    project: str,
    model_id: str,
    in_data: Optional[str],
    ref_data: Tuple[str],
    output: str,
    runner: str,
    model_path: str,
    action: Optional[str],
    param: List[Tuple[str, str]],
):
    """
    Generic run command.
    """

    runner = _check_model_and_runner(sc, runner)
    _validate_params(sc, runner, output, model_path, None)

    # The idea here:
    # - in_data is what the CLI argument was
    # - data_source is what is going to be sent to the API in the model config
    data_source = None
    if in_data:
        data_source = _configure_data_source(sc, in_data, runner)

    # The idea here:
    # - ref_data is what the CLI arguments were
    # - config_ref_data is what is going to be sent to the API in the model config
    ref_data = ref_data_factory(ref_data)
    config_ref_data = _configure_ref_data(sc, deepcopy(ref_data), runner)

    extra_params = None
    if param and len(param) > 0:
        extra_params = {key: value for key, value in param}

    create_and_run_record_handler(
        sc,
        params=extra_params,
        action=action,
        in_data=in_data,
        data_source=data_source,
        ref_data=ref_data,
        config_ref_data=config_ref_data,
        runner=runner,
        output=output,
        status_strings=get_model_type_config().run_status_descriptions,
        model_path=model_path,
    )


@records.command(help="Download all record handler associated artifacts.")
@click.option(
    "--output",
    metavar="DIR",
    help="Specify the output directory to download record handler artifacts to.",
    default=".",
)
@project_option
@click.option(
    "--model-id",
    metavar="ID",
    help="Specify the model.",
    required=False,
    callback=get_model,
    default="None",
)
@record_handler_option
@pass_session
def get(
    sc: SessionContext, record_handler_id: str, model_id: str, project: str, output: str
):
    if model_id == "None":
        raise click.BadOptionUsage(
            "--model-id", "Please specify the option '--model-id'."
        )
    record_handler: RecordHandler = sc.project.get_model(model_id).get_record_handler(
        record_handler_id
    )
    if record_handler.status != "completed":
        sc.log.error(
            f"""
                Cannot download record handler artifacts. Record handler should be in a completed
                state, but is instead {record_handler.status}."""
        )
        sc.exit(1)
    record_handler.download_artifacts(output)
    sc.log.info("Done fetching record handler artifacts.")
