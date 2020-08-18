# ensure absolute import for python3
from __future__ import absolute_import

import copy
import time

from blinker import Signal
from cloudaux.aws.iam import list_roles, list_users
from cloudaux.aws.sts import boto3_cached_conn
from cloudaux.aws.decorators import rate_limited

from aardvark.models.sqlalchemy import db_session


class AccountToUpdate(object):
    on_ready = Signal()
    on_complete = Signal()
    on_error = Signal()
    on_failure = Signal()

    def __init__(self, current_app, account_number, role_name, arns_list):
        self.current_app = current_app
        self.account_number = account_number
        self.role_name = role_name
        self.arn_list = arns_list
        self.conn_details = {
            "account_number": account_number,
            "assume_role": role_name,
            "session_name": "aardvark",
            "region": self.current_app.config.get("REGION") or "us-east-1",
            "arn_partition": self.current_app.config.get("ARN_PARTITION") or "aws",
        }
        self.max_access_advisor_job_wait = (
            5 * 60
        )  # Wait 5 minutes before giving up on jobs

    def update_account(self):
        """
        Updates Access Advisor data for a given AWS account.
        1) Gets list of IAM Role ARNs in target account.
        2) Gets IAM credentials in target account.
        3) Calls GenerateServiceLastAccessedDetails for each role
        4) Calls GetServiceLastAccessedDetails for each role to retrieve data

        :return: Return code and JSON Access Advisor data for given account
        """
        self.on_ready.send(self)
        arns = self._get_arns()

        if not arns:
            self.current_app.logger.warn("Zero ARNs collected. Exiting")
            exit(-1)

        client = self._get_client()
        try:
            details = self._call_access_advisor(client, list(arns))
        except Exception as e:
            self.on_failure.send(self, error=e)
            self.current_app.logger.exception(
                "Failed to call access advisor", exc_info=True
            )
            return 255, None
        else:
            self.on_complete.send(self)
            return 0, details

    def _get_arns(self):
        """
        Gets a list of all Role ARNs in a given account, optionally limited by
        class property ARN filter
        :return: list of role ARNs
        """
        client = boto3_cached_conn("iam", service_type="client", **self.conn_details)

        account_arns = set()

        for role in list_roles(**self.conn_details):
            account_arns.add(role["Arn"])

        for user in list_users(**self.conn_details):
            account_arns.add(user["Arn"])

        for page in client.get_paginator("list_policies").paginate(Scope="Local"):
            for policy in page["Policies"]:
                account_arns.add(policy["Arn"])

        for page in client.get_paginator("list_groups").paginate():
            for group in page["Groups"]:
                account_arns.add(group["Arn"])

        result_arns = set()
        for arn in self.arn_list:
            if arn.lower() == "all":
                return account_arns

            if arn not in account_arns:
                self.current_app.logger.warn(
                    "Provided ARN {arn} not found in account.".format(arn=arn)
                )
                continue

            result_arns.add(arn)

        return list(result_arns)

    def _get_client(self):
        """
        Assumes into the target account and obtains IAM client

        :return: boto3 IAM client in target account & role
        """
        client = boto3_cached_conn("iam", **self.conn_details)
        return client

    def _call_access_advisor(self, iam, arns):
        jobs = self._generate_job_ids(iam, arns)
        details = self._get_job_results(iam, jobs)
        if arns and not details:
            self.current_app.error("Didn't get any results from Access Advisor")
        return details

    @rate_limited()
    def _generate_service_last_accessed_details(self, iam, arn):
        """ Wrapping the actual AWS API calls for rate limiting protection. """
        return iam.generate_service_last_accessed_details(Arn=arn)["JobId"]

    @rate_limited()
    def _get_service_last_accessed_details(self, iam, job_id):
        """ Wrapping the actual AWS API calls for rate limiting protection. """
        return iam.get_service_last_accessed_details(JobId=job_id)

    def _generate_job_ids(self, iam, arns):
        jobs = {}
        for role_arn in arns:
            try:
                job_id = self._generate_service_last_accessed_details(iam, role_arn)
                jobs[job_id] = role_arn
            except iam.exceptions.NoSuchEntityException:
                """ We're here because this ARN disappeared since the call to self._get_arns(). Log the missing ARN and move along.  """
                self.current_app.logger.info(
                    "ARN {arn} found gone when fetching details".format(arn=role_arn)
                )
            except Exception as e:
                self.on_error.send(self, error=e)
                self.current_app.logger.error(
                    "Could not gather data from {0}.".format(role_arn), exc_info=True
                )
        return jobs

    def _get_job_results(self, iam, jobs):
        access_details = {}
        job_queue = list(jobs.keys())
        last_job_completion_time = time.time()

        while job_queue:

            # Check for timeout
            now = time.time()
            if now - last_job_completion_time > self.max_access_advisor_job_wait:
                # We ran out of time, some jobs are unfinished
                self._log_unfinished_jobs(job_queue, jobs)
                break

            # Pull next job ID
            job_id = job_queue.pop()
            role_arn = jobs[job_id]
            try:
                details = self._get_service_last_accessed_details(iam, job_id)
            except Exception as e:
                self.on_error.send(self, error=e)
                self.current_app.logger.error(
                    "Could not gather data from {0}.".format(role_arn), exc_info=True
                )
                continue

            # Check job status
            if details["JobStatus"] == "IN_PROGRESS":
                job_queue.append(job_id)
                if (
                    not job_queue
                ):  # We're hanging on the last job, let's hang back for a bit
                    time.sleep(1)
                continue

            # Check for job failure
            if details["JobStatus"] != "COMPLETED":
                log_str = "Job {job_id} finished with unexpected status {status} for ARN {arn}.".format(
                    job_id=job_id, status=details["JobStatus"], arn=role_arn
                )

                failing_arns = self.current_app.config.get("FAILING_ARNS", {})
                if role_arn in failing_arns:
                    self.current_app.logger.info(log_str)
                else:
                    self.current_app.logger.error(log_str)

                continue

            # Job status must be COMPLETED. Save result.
            last_job_completion_time = time.time()
            updated_list = []

            for detail in details.get("ServicesLastAccessed"):
                # create a copy, we're going to modify the time to epoch
                updated_item = copy.copy(detail)

                # AWS gives a datetime, convert to epoch
                last_auth = detail.get("LastAuthenticated")
                if last_auth:
                    last_auth = int(time.mktime(last_auth.timetuple()) * 1000)
                else:
                    last_auth = 0

                updated_item["LastAuthenticated"] = last_auth
                updated_list.append(updated_item)

            access_details[role_arn] = updated_list

        return access_details

    def _log_unfinished_jobs(self, job_queue, job_details):
        for job_id in job_queue:
            role_arn = job_details[job_id]
            self.current_app.logger.error(
                "Job {job_id} for ARN {arn} didn't finish".format(
                    job_id=job_id, arn=role_arn,
                )
            )


def persist_aa_data(app, aa_data):
    """
    Reads access advisor JSON file & persists to our database
    """
    from aardvark.models.sqlalchemy.advisor_data import AdvisorData
    from aardvark.models.sqlalchemy.aws_iam_object import AWSIAMObject

    with db_session as session:
        if not aa_data:
            app.logger.warn(
                "Cannot persist Access Advisor Data as no data was collected."
            )
            return

        arn_cache = {}
        for arn, data in aa_data.items():
            if arn in arn_cache:
                item = arn_cache[arn]
            else:
                item = AWSIAMObject.get_or_create(arn, session=session)
                arn_cache[arn] = item
            for service in data:
                AdvisorData.create_or_update(
                    item.id,
                    service["LastAuthenticated"],
                    service["ServiceName"],
                    service["ServiceNamespace"],
                    service.get("LastAuthenticatedEntity"),
                    service["TotalAuthenticatedEntities"],
                    session=session,
                )