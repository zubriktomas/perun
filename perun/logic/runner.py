"""Collection of functions for running collectors and postprocessors"""

import os
import subprocess

import distutils.util as dutils
import perun.vcs as vcs
import perun.logic.config as config
import perun.logic.store as store
import perun.logic.commands as commands
import perun.profile.factory as profile
import perun.utils as utils
import perun.utils.log as log
from perun.logic.pcs import pass_pcs, PCS
from perun.utils import get_module
from perun.utils.helpers import COLLECT_PHASE_COLLECT, COLLECT_PHASE_POSTPROCESS, \
    COLLECT_PHASE_CMD, COLLECT_PHASE_WORKLOAD, CollectStatus, PostprocessStatus, \
    Job, Unit

__author__ = 'Tomas Fiedor'


def construct_job_matrix(cmd, args, workload, collector, postprocessor, **kwargs):
    """Constructs the job matrix represented as dictionary.

    Reads the local of the current PCS and constructs the matrix of jobs
    that will be run. Each job consists of command that will be run,
    collector used to collect the data and list of postprocessors to
    alter the output profiles. Inside the dictionary jobs are distributed
    by binaries, then workloads and finally Jobs.

    Returns the job matrix as dictionary of form:
    {
      'cmd1': {
        'workload1': [ Job1, Job2 , ...],
        'workload2': [ Job1, Job2 , ...]
      },
      'cmd2': {
        'workload1': [ Job1, Job2 , ...],
        'workload2': [ Job1, Job2 , ...]
      }
    }

    Arguments:
        cmd(str): binary that will be run
        args(str): lists of additional arguments to the job
        workload(list): list of workloads
        collector(list): list of collectors
        postprocessor(list): list of postprocessors
        kwargs(dict): additional parameters issued from the command line

    Returns:
        dict, int: dict of jobs in form of {cmds: {workloads: {Job}}}, number of jobs
    """
    def construct_unit(unit, unit_type, ukwargs):
        """Helper function for constructing the {'name', 'params'} objects for collectors and posts.

        Arguments:
            unit(str): name of the unit (collector/postprocessor)
            unit_type(str): name of the unit type (collector or postprocessor)
            ukwargs(dict): dictionary of additional parameters

        Returns:
            dict: dictionary of the form {'name', 'params'}
        """
        # Get the dictionaries for from string and from file params obtained from commandline
        unit_param_dict = ukwargs.get(unit_type + "_params", {}).get(unit, {})

        # Construct the object with name and parameters
        return Unit(unit, unit_param_dict)

    # Convert the bare lists of collectors and postprocessors to {'name', 'params'} objects
    collector_pairs = list(map(lambda c: construct_unit(c, 'collector', kwargs), collector))
    postprocessors = list(map(lambda p: construct_unit(p, 'postprocessor', kwargs), postprocessor))

    # Construct the actual job matrix
    matrix = {
        str(b): {
            str(w): [
                Job(c, postprocessors, str(b), str(w), str(a))
                    for c in collector_pairs for a in args or ['']
                ] for w in workload
            } for b in cmd
        }

    # Count overall number of the jobs:
    number_of_jobs = 0
    for cmd_values in matrix.values():
        for workload_values in cmd_values.values():
            for job in workload_values:
                number_of_jobs += 1 + len(job.postprocessors)

    return matrix, number_of_jobs


def load_job_info_from_config(pcs):
    """
    Arguments:
        pcs(PCS): object with performance control system wrapper

    Returns:
        dict: dictionary with cmds, args, workloads, collectors and postprocessors
    """
    local_config = pcs.local_config().data

    if 'collectors' not in local_config.keys():
        log.error("missing 'collector' in the local.yml")
    collectors = local_config['collectors']
    postprocessors = local_config.get('postprocessors', [])

    if 'cmds' not in local_config.keys():
        log.error("missing 'cmds' section in local.yml")

    info = {
        'cmd': local_config['cmds'],
        'workload': local_config.get('workloads', ['']),
        'postprocessor': [post.get('name', '') for post in postprocessors],
        'collector': [collect.get('name', '') for collect in collectors],
        'args': local_config['args'] if 'args' in local_config.keys() else [],
        'collector_params': {
            collect.get('name', ''): collect.get('params', {}) for collect in collectors
            },
        'postprocessor_params': {
            post.get('name', ''): post.get('params', {}) for post in postprocessors
            }
    }

    return info


def is_status_ok(returned_status, expected_status):
    """Helper function for checking the status of the runners.

    Since authors of the collectors and processors may not behave well, we need
    this function to check either for the expected value of the enum (if they return int
    instead of enum) or enum if they return politely enum.

    Arguments:
        returned_status(int or Enum): status returned from the collector
        expected_status(Enum): expected status

    Returns:
        bool: true if the status was 0, CollectStatus.OK or PostprocessStatus.OK
    """
    return returned_status == expected_status or returned_status == expected_status.value


def run_all_phases_for(runner, runner_type, runner_params):
    """Run all of the phases (before, runner_type, after) for given params.

    Runs three of the phases before, runner_type and after for the given runner params
    with runner (collector or postprocesser). During each phase, either error occurs,
    with given error message or updated params, that are used in next phase. This
    way the phases can pass the information.

    Returns the computed profile

    Arguments:
        runner(module): module that is going to be runned
        runner_type(str): string type of the runner (either collector or postprocessor)
        runner_params(dict): dictionary of arguments for runner
    """
    ok_status = CollectStatus.OK if runner_type == 'collector' else PostprocessStatus.OK
    error_status = CollectStatus.ERROR if runner_type == 'collector' else PostprocessStatus.ERROR
    runner_verb = runner_type[:-2]

    for phase in ['before', runner_verb, 'after']:
        phase_function = getattr(runner, phase, None)
        if phase_function:
            try:
                ret_val, ret_msg, updated_params = phase_function(**runner_params)
            # We safely catch all of the exceptions
            except Exception as exc:
                ret_val, ret_msg, updated_params = error_status, str(exc), {}
            runner_params.update(updated_params or {})
            if not is_status_ok(ret_val, ok_status):
                return error_status, "error while {}{} phase: {}".format(
                    phase, ("_" + runner_verb)*(phase != runner_verb), ret_msg
                ), {}
        elif phase == runner_verb:
            return error_status, "missing {}() function for {}".format(
                runner_verb, runner.__name__
            ), {}

    # Return the processed profile
    if 'profile' not in runner_params.keys():
        return error_status, "missing generated profile for {} {}".format(
            runner_type, runner.__name__
        ), {}
    return ok_status, "", runner_params['profile']


def run_collector(collector, job):
    """Run the job of collector of the given name.

    Tries to look up the module containing the collector specified by the
    collector name, and then runs it with the parameters and returns collected profile.

    Arguments:
        collector(Unit): object representing the collector
        job(Job): additional information about the running job

    Returns:
        (int, dict): status of the collection, generated profile
    """
    log.print_current_phase(
        "Collecting data by {}", collector.name, COLLECT_PHASE_COLLECT
    )

    try:
        collector_module = get_module('perun.collect.{0}.run'.format(collector.name))
    except ImportError:
        return CollectStatus.ERROR, "{} does not exist".format(collector.name), {}

    # First init the collector by running the before phases (if it has)
    job_params = utils.merge_dictionaries(job._asdict(), collector.params)
    collection_status, collection_msg, prof \
        = run_all_phases_for(collector_module, 'collector', job_params)

    if collection_status != CollectStatus.OK:
        log.error(collection_msg, recoverable=True)
    else:
        print("Successfully collected data from {}".format(job.cmd))

    return collection_status, prof


def run_collector_from_cli_context(ctx, collector_name, collector_params):
    """Runs the collector according to the given cli context.

    This is used as a wrapper for calls from various collector modules. This was extracted,
    to minimize the needed input of new potential collectors.

    Arguments:
        ctx(Context): click context containing arguments obtained by 'perun collect' command
        collector_name(str): name of the collector that will be run
        collector_params(dict): dictionary with collector params
    """
    try:
        cmd, args, workload = ctx.obj['cmd'], ctx.obj['args'], ctx.obj['workload']
        minor_versions = ctx.obj['minor_version_list']
        collector_params.update(ctx.obj['params'])
        run_single_job(cmd, args, workload, [collector_name], [], minor_versions, **{
            'collector_params': {collector_name: collector_params}
        })
    except KeyError as collector_exception:
        log.error("missing parameter: {}".format(collector_exception))


def run_postprocessor(postprocessor, job, prof):
    """Run the job of postprocess of the given name.

    Tries to look up the module containing the postprocessor specified by the
    postprocessor name, and then runs it with the parameters and returns processed
    profile.

    Arguments:
        postprocessor(Unit): dictionary representing the postprocessor
        job(Job): additional information about the running job
        prof(dict): dictionary with profile

    Returns:
        (int, dict): status of the collection, postprocessed profile
    """
    log.print_current_phase(
        "Postprocessing data with {}", postprocessor.name, COLLECT_PHASE_POSTPROCESS
    )

    try:
        postprocessor_module = get_module('perun.postprocess.{0}.run'.format(postprocessor.name))
    except ImportError:
        return PostprocessStatus.ERROR, "{} does not exist".format(postprocessor.name), {}

    # First init the collector by running the before phases (if it has)
    job_params = utils.merge_dict_range(job._asdict(), {'profile': prof}, postprocessor.params)
    post_status, post_msg, prof \
        = run_all_phases_for(postprocessor_module, 'postprocessor', job_params)

    if post_status != PostprocessStatus.OK:
        log.error(post_msg)
    else:
        print("Successfully postprocessed data by {}".format(postprocessor.name))

    return post_status, prof


def store_generated_profile(pcs, prof, job):
    """Stores the generated profile in the pending jobs directory.

    :param PCS pcs: object with performance control system wrapper
    :param dict prof: profile that we are storing in the repository
    :param Job job: job with additional information about generated profiles
    """
    full_profile = profile.finalize_profile_for_job(pcs, prof, job)
    full_profile_name = profile.generate_profile_name(full_profile)
    profile_directory = pcs.get_job_directory()
    full_profile_path = os.path.join(profile_directory, full_profile_name)
    profile.store_profile_at(full_profile, full_profile_path)
    log.info("stored profile at: {}".format(os.path.relpath(full_profile_path)))
    if dutils.strtobool(str(config.lookup_key_recursively("profiles.register_after_run", "false"))):
        commands.add([full_profile_path], prof['origin'], keep_profile=False)


def run_postprocessor_on_profile(prof, postprocessor_name, postprocessor_params):
    """Run the job of the postprocessor according to the given profile.

    First extracts the information from the profile in order to construct the job,
    then runs the given postprocessor that is appended to the list of postprocessors
    of the profile, and the postprocessed profile is stored in the pending jobs.

    Arguments:
        prof(dict): dictionary with profile informations
        postprocessor_name(str): name of the postprocessor that we are using
        postprocessor_params(dict): parameters for the postprocessor

    Returns:
        PostprocessStatus: status how the postprocessing went
    """
    pcs = PCS(store.locate_perun_dir_on(os.getcwd()))
    profile_job = profile.extract_job_from_profile(prof)
    postprocessor_unit = Unit(postprocessor_name, postprocessor_params)
    profile_job.postprocessors.append(postprocessor_unit)

    p_status, processed_profile = run_postprocessor(postprocessor_unit, profile_job, prof)
    if p_status == PostprocessStatus.OK and prof:
        store_generated_profile(pcs, processed_profile, profile_job)
    return p_status


@pass_pcs
def run_prephase_commands(pcs, phase, phase_colour='white'):
    """Runs the phase before the actual collection of the methods

    This command first retrieves the phase from the configuration, and runs
    safely all of the commands specified in the list.

    The phase is specified in :doc:`config` by keys specified in section
    :cunit:`execute`.

    :param PCS pcs: performance control system
    :param str phase: name of the phase commands
    :param str phase_colour: colour for the printed phase
    """
    phase_key = ".".join(["execute", phase]) if not phase.startswith('execute') else phase
    cmds = pcs.local_config().safe_get(phase_key, [])
    if cmds:
        log.cprint("Running '{}' phase".format(phase), phase_colour)
        print("")
        try:
            utils.run_safely_list_of_commands(cmds)
        except subprocess.CalledProcessError as e:
            error_command = str(e.cmd)
            error_code = e.returncode
            error_output = e.output
            log.error("error in {} phase while running '{}' exited with: {} ({})".format(
                phase, error_command, error_code, error_output
            ))


def run_jobs_on_current_working_dir(pcs, job_matrix, number_of_jobs):
    """Runs the batch of jobs on current state of the VCS.

    This function expects no changes not commited in the repo, it excepts correct version
    checked out and just runs the matrix.

    :param PCS pcs: object with performance control system wrapper
    :param dict job_matrix: dictionary with jobs that will be run
    :param int number_of_jobs: number of jobs that will be run
    """
    log.print_job_progress.current_job = 1
    for job_cmd, workloads_per_cmd in job_matrix.items():
        log.print_current_phase("Collecting profiles for {}", job_cmd, COLLECT_PHASE_CMD)
        for workload, jobs_per_workload in workloads_per_cmd.items():
            log.print_current_phase(" - processing workload {}", workload, COLLECT_PHASE_WORKLOAD)
            for job in jobs_per_workload:
                log.print_job_progress(number_of_jobs)

                # Run the collector and check if the profile was successfully collected
                # In case, the status was not OK, then we skip the postprocessing
                c_status, prof = run_collector(job.collector, job)
                if c_status != CollectStatus.OK or not prof:
                    continue

                # Temporary nasty hack
                prof = profile.finalize_profile_for_job(pcs, prof, job)

                for postprocessor in job.postprocessors:
                    log.print_job_progress(number_of_jobs)
                    # Run the postprocessor and check if the profile was successfully postprocessed
                    p_status, prof = run_postprocessor(postprocessor, job, prof)
                    if p_status != PostprocessStatus.OK or not prof:
                        continue

                # Store the computed profile inside the job directory
                store_generated_profile(pcs, prof, job)


def run_jobs(pcs, minor_version_list, job_matrix, number_of_jobs):
    """
    Arguments:
    :param PCS pcs: object with performance control system wrapper
    :param list minor_version_list: list of MinorVersion info
    :param dict job_matrix: dictionary with jobs that will be run
    :param int number_of_jobs: number of jobs that will be run
    """
    with vcs.CleanState(pcs.vcs_type, pcs.vcs_path):
        for minor_version in minor_version_list:
            vcs.checkout(pcs.vcs_type, pcs.vcs_path, minor_version.checksum)
            run_prephase_commands('pre_run', COLLECT_PHASE_CMD)
            run_jobs_on_current_working_dir(pcs, job_matrix, number_of_jobs)


def run_jobs_with_history(pcs, minor_version_list, job_matrix, number_of_jobs):
    """
    Arguments:
    :param PCS pcs: object with performance control system wrapper
    :param list minor_version_list: list of MinorVersion info
    :param dict job_matrix: dictionary with jobs that will be run
    :param int number_of_jobs: number of jobs that will be run
    """
    with log.History(minor_version_list[0].checksum) as history:
        with vcs.CleanState(pcs.vcs_type, pcs.vcs_path):
            for minor_version in minor_version_list:
                history.progress_to_next_minor_version(minor_version)
                print("")
                history.finish_minor_version(minor_version, [])
                vcs.checkout(pcs.vcs_type, pcs.vcs_path, minor_version.checksum)
                run_prephase_commands('pre_run', COLLECT_PHASE_CMD)
                run_jobs_on_current_working_dir(pcs, job_matrix, number_of_jobs)
                print("")
                history.flush(with_border=True)


@pass_pcs
def run_single_job(pcs, cmd, args, workload, collector, postprocessor, minor_version_list,
                   with_history=False, **kwargs):
    """
    :param PCS pcs: object with performance control system wrapper
    :param str cmd: cmdary that will be run
    :param str args: lists of additional arguments to the job
    :param list workload: list of workloads
    :param list collector: list of collectors
    :param list postprocessor: list of postprocessors
    :param list minor_version_list: list of MinorVersion info
    :param bool with_history: if set to true, then we will print the history object
    :param dict kwargs: dictionary of additional params for postprocessor and collector
    """
    job_matrix, number_of_jobs = \
        construct_job_matrix(cmd, args, workload, collector, postprocessor, **kwargs)
    runner_function = run_jobs_with_history if with_history else run_jobs
    runner_function(pcs, minor_version_list, job_matrix, number_of_jobs)


@pass_pcs
def run_matrix_job(pcs, minor_version_list, with_history=False):
    """
    :param PCS pcs: object with performance control system wrapper
    :param list minor_version_list: list of MinorVersion info
    :param bool with_history: if set to true, then we will print the history object
    """
    job_matrix, number_of_jobs = construct_job_matrix(**load_job_info_from_config(pcs))
    runner_function = run_jobs_with_history if with_history else run_jobs
    runner_function(pcs, minor_version_list, job_matrix, number_of_jobs)

