"""
KBase Job Manager

The main class here defines a manager for running jobs (as Job objects).
This class knows how to fetch job status, kill jobs, etc.
It also communicates with the front end over the KBaseJobs channel.

It is intended for use as a singleton - use the get_manager() function
to fetch it.
"""
__author__ = "Bill Riehl <wjriehl@lbl.gov>"
__version__ = "0.0.1"

import biokbase.narrative.clients as clients
from .job import Job
from ipykernel.comm import Comm
import threading
import json
import logging
from biokbase.narrative.common import kblogging
from biokbase.narrative.common.log_common import EVENT_MSG_SEP
from IPython.display import HTML
from jinja2 import Template
import dateutil.parser
import datetime
from biokbase.narrative.app_util import system_variable

class JobManager(object):
    """
    The KBase Job Manager clsas. This handles all jobs and makes their status available.
    On status lookups, it feeds the results to the KBaseJobs channel that the front end
    listens to.
    """
    __instance = None

    # keys = job_id, values = { refresh = T/F, job = Job object }
    _running_jobs = dict()

    _lookup_timer = None
    _comm = None
    _log = kblogging.get_logger(__name__)
    _log.setLevel(logging.INFO)

    def __new__(cls):
        if JobManager.__instance is None:
            JobManager.__instance = object.__new__(cls)
        return JobManager.__instance

    def initialize_jobs(self):
        """
        Initializes this JobManager.
        This is expected to be run by a running Narrative, and naturally linked to a workspace.
        So it does the following steps.
        1. app_util.system_variable('workspace_id')
        2. get list of jobs with that ws id from UJS (also gets tag, cell_id, run_id)
        3. initialize the Job objects by running NJS.get_job_params on each of those (also gets app_id)
        4. start the status lookup loop.
        """
        ws_id = system_variable('workspace_id')
        try:
            nar_jobs = clients.get('user_and_job_state').list_jobs2({
                'authstrat': 'kbaseworkspace',
                'authparams': [str(ws_id)]
            })
        except Exception as e:
            kblogging.log_event(self._log, 'init_error', {'err': str(e)})
            self._send_comm_message('job_init_err', {'msg': 'Unable to get jobs list!', 'err': str(e)})
            raise

        for info in nar_jobs:
            job_id = info[0]
            user_info = info[1]
            job_meta = info[10]
            job_info = clients.get('job_service').get_job_params(job_id)[0]
            try:
                self._running_jobs[job_id] = {
                    'refresh': True,
                    'job': Job.from_state(job_id, job_info, user_info[0], app_id=job_info.get('app_id'), tag=job_meta.get('tag', 'release'), cell_id=job_meta.get('cell_id', None))
                }
            except Exception as e:
                kblogging.log_event(self._log, 'init_error', {'err': str(e)})
                self._send_comm_message('job_init_lookup_err', {'msg': 'Unable to get job info!', 'job_id': job_id, 'err': str(e)})
        # only keep one loop at a time in cause this gets called again!
        if self._lookup_timer is not None:
            self._lookup_timer.cancel()
        self._lookup_job_status_loop()

    def list_jobs(self):
        """
        List all job ids, their info, and status in a quick HTML format.
        """
        try:
            status_set = list()
            for job_id in self._running_jobs:
                job = self._running_jobs[job_id]['job']
                job_state = job.state()
                job_params = job.parameters()
                job_state['app_id'] = job_params[0].get('app_id', 'Unknown App')
                job_state['owner'] = job.owner
                status_set.append(job_state)
            if not len(status_set):
                return "No running jobs!"
            status_set = sorted(status_set, key=lambda s: s['creation_time'])
            for i in range(len(status_set)):
                status_set[i]['creation_time'] = datetime.datetime.strftime(datetime.datetime.fromtimestamp(status_set[i]['creation_time']/1000), "%Y-%m-%d %H:%M:%S")
                exec_start = status_set[i].get('exec_start_time', None)
                if 'complete_time' in status_set[i]:
                    status_set[i]['complete_time'] = datetime.datetime.strftime(datetime.datetime.fromtimestamp(status_set[i]['complete_time']/1000), "%Y-%m-%d %H:%M:%S")
                    finished = status_set[i].get('finish_time', None)
                    if finished and exec_start:
                        delta = datetime.datetime.fromtimestamp(finished/1000.0) - datetime.datetime.fromtimestamp(exec_start/1000.0)
                        delta = delta - datetime.timedelta(microseconds=delta.microseconds)
                        status_set[i]['run_time'] = str(delta)
                elif exec_start:
                    delta = datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(exec_start/1000.0)
                    delta = delta - datetime.timedelta(microseconds=delta.microseconds)
                    status_set[i]['run_time'] = str(delta)
                else:
                    status_set[i]['run_time'] = 'Not started'

            tmpl = """
            <table class="table table-bordered table-striped table-condensed">
                <tr>
                    <th>Id</th>
                    <th>Name</th>
                    <th>Submitted</th>
                    <th>Submitted By</th>
                    <th>Status</th>
                    <th>Run Time</th>
                    <th>Complete Time</th>
                </tr>
                {% for j in jobs %}
                <tr>
                    <td>{{ j.job_id|e }}</td>
                    <td>{{ j.app_id|e }}</td>
                    <td>{{ j.creation_time|e }}</td>
                    <td>{{ j.owner|e }}</td>
                    <td>{{ j.job_state|e }}</td>
                    <td>{{ j.run_time|e }}</td>
                    <td>{% if j.complete_time %}{{ j.complete_time|e }}{% else %}Incomplete{% endif %}</td>
                </tr>
                {% endfor %}
            </table>
            """
            return HTML(Template(tmpl).render(jobs=status_set))

        except Exception as e:
            kblogging.log_event(self._log, "list_jobs.error", {'err': str(e)})
            raise

    def get_jobs_list(self):
        """
        A convenience method for fetching an unordered list of all running Jobs.
        """
        return [j['job'] for j in self._running_jobs.values()]

    def _get_existing_job(self, job_tuple):
        """
        creates a Job object from a job_id that already exists.
        If no job exists, raises an Exception.

        Parameters:
        -----------
        job_tuple : The expected 4-tuple representing a Job. The format is:
            (job_id, set of job inputs (as JSON), version tag, cell id that started the job)
        """

        # remove the prefix (if present) and take the last element in the split
        job_id = job_tuple[0].split(':')[-1]
        try:
            job_info = clients.get('job_service').get_job_params(job_id)[0]
            return Job.from_state(job_id, job_info, app_id=job_tuple[1], tag=job_tuple[2], cell_id=job_tuple[3])
        except Exception as e:
            kblogging.log_event(self._log, "get_existing_job.error", {'job_id': job_id, 'err': str(e)})
            raise

    def _lookup_job_status(self, job_id):
        """
        Will raise a ValueError if job_id doesn't exist.
        Sends the status over the comm channel as the usual job_status message.
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError('job "{}" not found!'.format(job_id))
        state = job.state()
        status = {job_id: {
                     'state': state,
                     'spec': job.app_spec(),
                     'widget_info': job.get_viewer_params(state),
                     'owner': job.owner
                 }}
        self._send_comm_message('job_status', status)


    def _lookup_all_job_status(self, ignore_refresh_flag=False):
        """
        Looks up status for all jobs.
        Once job info is acquired, it gets pushed to the front end over the
        'KBaseJobs' channel.
        """
        status_set = dict()
        # grab the list of running job ids, so we don't run into update-while-iterating problems.
        running_job_ids = self._running_jobs.keys()
        for job_id in running_job_ids:
            try:
                job = self._running_jobs[job_id]
                if job['refresh'] or ignore_refresh_flag:
                    state = job['job'].state()
                    status_set[job_id] = {'state': state,
                                          'spec': job['job'].app_spec(),
                                          'widget_info': job['job'].get_viewer_params(state),
                                          'owner': job['job'].owner}
            except Exception as e:
                self._log.setLevel(logging.ERROR)
                kblogging.log_event(self._log, "lookup_job_status.error", {'err': str(e)})
                self._send_comm_message('job_err', {
                    'job_id': job_id,
                    'message': str(e)
                })
        self._send_comm_message('job_status_all', status_set)

    def _lookup_job_status_loop(self):
        """
        Initialize a loop that will look up job info. This uses a Timer thread on a 10
        second loop to update things.
        """
        self._lookup_all_job_status()
        self._lookup_timer = threading.Timer(10, self._lookup_job_status_loop)
        self._lookup_timer.start()

    def cancel_job_lookup_loop(self):
        """
        Cancels a running timer if one's still alive.
        """
        if self._lookup_timer:
            self._lookup_timer.cancel()

    def register_new_job(self, job):
        """
        Registers a new Job with the manager - should only be invoked when a new Job gets
        started. This stores the Job locally and pushes it over the comm channel to the
        Narrative where it gets serialized.

        Parameters:
        -----------
        job : biokbase.narrative.jobs.job.Job object
            The new Job that was started.
        """
        self._running_jobs[job.job_id] = {'job': job, 'refresh': True}
        # push it forward! create a new_job message.
        self._lookup_job_status(job.job_id)
        self._send_comm_message('new_job', {})

    def get_job(self, job_id):
        """
        Returns a Job with the given job_id.
        Raises a ValueError if not found.
        """
        if job_id in self._running_jobs:
            return self._running_jobs[job_id]['job']
        else:
            raise ValueError('No job present with id {}'.format(job_id))

    def _handle_comm_message(self, msg):
        """
        Handles comm messages that come in from the other end of the KBaseJobs channel.
        All messages (of any use) should have a 'request_type' property.
        Possible types:
        * all_status
            refresh all jobs that are flagged to be looked up. Will send a
            message back with all lookup status.
        * job_status
            refresh the single job given in the 'job_id' field. Sends a message
            back with that single job's status, or an error message.
        * stop_update_loop
            stop the running refresh loop, if there's one going (might be
            one more pass, depending on the thread state)
        * start_update_loop
            reinitialize the refresh loop.
        * stop_job_update
            flag the given job id (should be an accompanying 'job_id' field) that the front
            end knows it's in a terminal state and should no longer have its status looked
            up in the refresh cycle.
        * start_job_update
            remove the flag that gets set by stop_job_update (needs an accompanying 'job_id'
            field)
        """

        if 'request_type' in msg['content']['data']:
            self._log.setLevel(logging.INFO)
            r_type = msg['content']['data']['request_type']
            job_id = msg['content']['data'].get('job_id', None)
            if job_id is not None and job_id not in self._running_jobs:
                # If it's not a real job, just silently ignore the request.
                # Maybe return an error? Yeah. Let's do that.
                self._send_comm_message('job_comm_error', {'job_id': job_id, 'message': 'Unknown job id', 'request_type': r_type})
                return

            if r_type == 'all_status':
                self._lookup_all_job_status(ignore_refresh_flag=True)

            elif r_type == 'job_status':
                if job_id is not None:
                    self._lookup_job_status(job_id)

            elif r_type == 'stop_update_loop':
                if self._lookup_timer is not None:
                    self._lookup_timer.cancel()

            elif r_type == 'start_update_loop':
                self._lookup_job_status_loop()

            elif r_type == 'stop_job_update':
                if job_id is not None:
                    self._running_jobs[job_id]['refresh'] = False

            elif r_type == 'start_job_update':
                if job_id is not None:
                    self._running_jobs[job_id]['refresh'] = True

            elif r_type == 'delete_job':
                if job_id is not None:
                    try:
                        self.delete_job(job_id)
                    except Exception as e:
                        self._send_comm_message('job_comm_error', {'message': str(e), 'request_type': r_type, 'job_id': job_id})

            elif r_type == 'cancel_job':
                if job_id is not None:
                    try:
                        self.cancel_job(job_id)
                    except Exception as e:
                        self._send_comm_message('job_comm_error', {'message': str(e), 'request_type': r_type, 'job_id': job_id})

            elif r_type == 'job_logs':
                if job_id is not None:
                    first_line = msg['content']['data'].get('first_line', 0)
                    num_lines = msg['content']['data'].get('num_lines', None)
                    self._get_job_logs(job_id, first_line=first_line, num_lines=num_lines)
                else:
                    raise ValueError('Need a job id to fetch jobs!')

            elif r_type == 'job_logs_latest':
                if job_id is not None:
                    num_lines = msg['content']['data'].get('num_lines', None)
                    self._get_latest_job_logs(job_id, num_lines=num_lines)

            else:
                self._send_comm_message('job_comm_error', {'message': 'Unknown message', 'request_type': r_type})
                raise ValueError('Unknown KBaseJobs message "{}"'.format(r_type))

    def _get_latest_job_logs(self, job_id, num_lines=None):
        job = self.get_job(job_id)
        if job is None:
            raise ValueError('job "{}" not found while fetching logs!'.format(job_id))

        (max_lines, logs) = job.log()

        first_line = 0
        if num_lines is not None and max_lines > num_lines:
            first_line = max_lines - num_lines
            logs = logs[first_line:]
        self._send_comm_message('job_logs', {'job_id': job_id, 'first': first_line, 'max_lines': max_lines, 'lines': logs, 'latest': True})


    def _get_job_logs(self, job_id, first_line=0, num_lines=None):
        job = self.get_job(job_id)
        if job is None:
            raise ValueError('job "{}" not found!'.format(job_id))

        (max_lines, log_slice) = job.log(first_line=first_line, num_lines=num_lines)
        self._send_comm_message('job_logs', {'job_id': job_id, 'first': first_line, 'max_lines': max_lines, 'lines': log_slice, 'latest': False})

    def delete_job(self, job_id):
        """
        If the job_id doesn't exist, raises a ValueError.
        Attempts to delete a job, and cancels it first. If the job cannot be canceled,
        raises an exception. If it can be canceled but not deleted, it gets canceled, then raises
        an exception.
        """
        if job_id is None:
            raise ValueError('Job id required for deletion!')

        try:
            self.cancel_job(job_id)
        except Exception as e:
            raise

        try:
            clients.get('user_and_job_state').delete_job(job_id)
        except Exception as e:
            raise

        del self._running_jobs[job_id]
        self._send_comm_message('job_deleted', {'job_id': job_id})

    def cancel_job(self, job_id):
        """
        Cancels a running job, placing it in a canceled state.
        Does NOT delete the job.
        Raises an exception if the current user doesn't have permission to cancel the job.
        """
        if job_id is None:
            raise ValueError('Job id required for cancellation!')
        if job_id not in self._running_jobs:
            raise ValueError('Attempting to cancel a Job that does not exist!')

        try:
            job = self.get_job(job_id)
            state = job.state()
            if state.get('cancelled', 0) == 1 or state.get('finished', 0) == 1:
                # It's already finished, don't try to cancel it again.
                return
        except Exception as e:
            raise ValueError('Unable to get Job state')

        try:
            clients.get('job_service').cancel_job({'job_id': job_id})
        except Exception as e:
            raise

        self._send_comm_message('job_canceled', {'job_id': job_id})

    def _send_comm_message(self, msg_type, content):
        """
        Sends a ipykernel.Comm message to the KBaseJobs channel with the given msg_type
        and content. These just get encoded into the message itself.
        """
        msg = {
            'msg_type': msg_type,
            'content': content
        }
        if self._comm is None:
            self._comm = Comm(target_name='KBaseJobs', data={})
            self._comm.on_msg(self._handle_comm_message)
        self._comm.send(msg)