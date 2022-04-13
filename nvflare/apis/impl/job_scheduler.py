# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Dict, List, Optional, Tuple

from nvflare.apis.fl_component import FLComponent
from nvflare.apis.fl_constant import ReturnCode
from nvflare.apis.fl_context import FLContext
from nvflare.apis.job_def import Job
from nvflare.apis.job_scheduler_spec import DispatchInfo, JobSchedulerSpec
from nvflare.apis.scheduler_constants import AuxChannelTopic, ShareableHeader
from nvflare.apis.server_engine_spec import ServerEngineSpec
from nvflare.apis.shareable import Shareable


class DefaultJobScheduler(JobSchedulerSpec, FLComponent):
    def __init__(
        self,
        client_req_timeout: float = 1.0,
        max_jobs: int = 10,
        check_resource_topic: str = AuxChannelTopic.CHECK_RESOURCE,
        cancel_resource_topic: str = AuxChannelTopic.CANCEL_RESOURCE,
    ):
        super().__init__()
        self.client_req_timeout = client_req_timeout
        self.max_jobs = max_jobs
        self.scheduled_jobs = []
        self.check_resource_topic = check_resource_topic
        self.cancel_resource_topic = cancel_resource_topic

    def _send_req_to_sites(
        self, request: Shareable, topic: str, sites: List[str], fl_ctx: FLContext
    ) -> Dict[str, Shareable]:
        engine = fl_ctx.get_engine()
        if not isinstance(engine, ServerEngineSpec):
            raise RuntimeError(f"engine inside fl_ctx should be of type ServerEngineSpec, but got {type(engine)}.")
        # result is {client_name: Shareable} of each site's result
        result = engine.send_aux_request(
            targets=sites, topic=topic, request=request, timeout=self.client_req_timeout, fl_ctx=fl_ctx
        )
        return result

    def _check_client_resources(self, resource_reqs: Dict[str, dict], fl_ctx: FLContext) -> Dict[str, Tuple[bool, str]]:
        """Checks resources on each site.

        Args:
            resource_reqs (dict): {client_name: resource_requirements}

        Returns:
            A dict of {client_name: client_check_result}
            where client_check_result is a tuple of {client check OK, resource reserve token if any}
        """
        result = {}

        for site_name, resource_requirements in resource_reqs:
            # assume server resource is unlimited
            if site_name == "server":
                continue
            request = Shareable()
            request.set_header(ShareableHeader.RESOURCE_SPEC, resource_requirements)
            site_name, shareable = self._send_req_to_sites(
                request=request, sites=[site_name], topic=self.check_resource_topic, fl_ctx=fl_ctx
            )
            if shareable.get_return_code() != ReturnCode.OK:
                result[site_name] = (False, None)
            else:
                result[site_name] = (
                    shareable.get_header(ShareableHeader.CHECK_RESOURCE_RESULT, False),
                    shareable.get_header(ShareableHeader.RESOURCE_RESERVE_TOKEN, None),
                )
        return result

    def _cancel_resources(
        self, resource_reqs: Dict[str, dict], resource_check_results: Dict[str, Tuple[bool, str]], fl_ctx: FLContext
    ):
        """Cancels any reserved resources based on resource check results.

        Args:
            resource_reqs (dict): {client_name: resource_requirements}
            resource_check_results: A dict of {client_name: client_check_result}
                where client_check_result is a tuple of {client check OK, resource reserve token if any}
            fl_ctx: FL context
        """
        for site_name, result in resource_check_results.items():
            check_result, token = result
            if check_result:
                request = Shareable()
                resource_requirements = resource_reqs[site_name]
                request.set_header(ShareableHeader.RESOURCE_RESERVE_TOKEN, token)
                request.set_header(ShareableHeader.RESOURCE_SPEC, resource_requirements)
                _ = self._send_req_to_sites(
                    request=request, sites=[site_name], topic=self.cancel_resource_topic, fl_ctx=fl_ctx
                )
        return False, None

    def _try_job(self, job: Job, fl_ctx) -> (bool, Optional[Dict[str, DispatchInfo]]):
        # we are assuming server resource is sufficient
        resource_check_results = self._check_client_resources(resource_reqs=job.resource_spec, fl_ctx=fl_ctx)

        if not resource_check_results:
            return False, None

        if len(resource_check_results) < job.min_sites:
            return self._cancel_resources(
                resource_reqs=job.resource_spec, resource_check_results=resource_check_results, fl_ctx=fl_ctx
            )

        required_sites_received = 0
        num_sites_ok = 0
        sites_dispatch_info = {}
        for site_name, check_result in resource_check_results.items():
            if check_result[0]:
                sites_dispatch_info[site_name] = DispatchInfo(
                    resource_requirements=job.resource_spec[site_name], token=check_result[1]
                )
                num_sites_ok += 1
                if site_name in job.required_sites:
                    required_sites_received += 1

        if num_sites_ok < job.min_sites:
            return self._cancel_resources(
                resource_reqs=job.resource_spec, resource_check_results=resource_check_results, fl_ctx=fl_ctx
            )

        if required_sites_received < len(job.required_sites):
            return self._cancel_resources(
                resource_reqs=job.resource_spec, resource_check_results=resource_check_results, fl_ctx=fl_ctx
            )

        return True, sites_dispatch_info

    def handle_event(self, event_type: str, fl_ctx: FLContext):
        if event_type in ["JOB_ABORTED", "JOB_COMPLETED", "JOB_CANCELED"]:
            job = fl_ctx.get_prop("job")
            self.scheduled_jobs.pop(job)

    def schedule_job(
        self, job_candidates: List[Job], fl_ctx: FLContext
    ) -> (Optional[Job], Optional[Dict[str, DispatchInfo]]):
        if len(self.scheduled_jobs) >= self.max_jobs:
            return None, None

        for job in job_candidates:
            ok, sites = self._try_job(job, fl_ctx)
            if ok:
                self.scheduled_jobs.append(job)
                return job, sites
        return None, None