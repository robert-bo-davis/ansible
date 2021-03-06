# Copyright (c) 2016 Matt Davis, <mdavis@ansible.com>
#                    Chris Houseknecht, <house@redhat.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import re
import sys
import copy
import inspect
import traceback

from os.path import expanduser

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six.moves import configparser
import ansible.module_utils.six.moves.urllib.parse as urlparse
try:
    from ansible.release import __version__ as ANSIBLE_VERSION
except ImportError:
    ANSIBLE_VERSION = 'unknown'

AZURE_COMMON_ARGS = dict(
    cli_default_profile=dict(type='bool'),
    profile=dict(type='str'),
    subscription_id=dict(type='str', no_log=True),
    client_id=dict(type='str', no_log=True),
    secret=dict(type='str', no_log=True),
    tenant=dict(type='str', no_log=True),
    ad_user=dict(type='str', no_log=True),
    password=dict(type='str', no_log=True),
    cloud_environment=dict(type='str'),
    # debug=dict(type='bool', default=False),
)

AZURE_CREDENTIAL_ENV_MAPPING = dict(
    cli_default_profile='AZURE_CLI_DEFAULT_PROFILE',
    profile='AZURE_PROFILE',
    subscription_id='AZURE_SUBSCRIPTION_ID',
    client_id='AZURE_CLIENT_ID',
    secret='AZURE_SECRET',
    tenant='AZURE_TENANT',
    ad_user='AZURE_AD_USER',
    password='AZURE_PASSWORD',
    cloud_environment='AZURE_CLOUD_ENVIRONMENT',
)

AZURE_TAG_ARGS = dict(
    tags=dict(type='dict'),
    append_tags=dict(type='bool', default=True),
)

AZURE_COMMON_REQUIRED_IF = [
    ('log_mode', 'file', ['log_path'])
]

ANSIBLE_USER_AGENT = 'Ansible/{0}'.format(ANSIBLE_VERSION)
CLOUDSHELL_USER_AGENT_KEY = 'AZURE_HTTP_USER_AGENT'
VSCODEEXT_USER_AGENT_KEY = 'VSCODEEXT_USER_AGENT'

CIDR_PATTERN = re.compile(r"(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1"
                          r"[0-9]{2}|2[0-4][0-9]|25[0-5])(/([0-9]|[1-2][0-9]|3[0-2]))")

AZURE_SUCCESS_STATE = "Succeeded"
AZURE_FAILED_STATE = "Failed"

HAS_AZURE = True
HAS_AZURE_EXC = None
HAS_AZURE_CLI_CORE = True

HAS_MSRESTAZURE = True
HAS_MSRESTAZURE_EXC = None

try:
    import importlib
except ImportError:
    # This passes the sanity import test, but does not provide a user friendly error message.
    # Doing so would require catching Exception for all imports of Azure dependencies in modules and module_utils.
    importlib = None

try:
    from packaging.version import Version
    HAS_PACKAGING_VERSION = True
    HAS_PACKAGING_VERSION_EXC = None
except ImportError as exc:
    Version = None
    HAS_PACKAGING_VERSION = False
    HAS_PACKAGING_VERSION_EXC = exc

# NB: packaging issue sometimes cause msrestazure not to be installed, check it separately
try:
    from msrest.serialization import Serializer
except ImportError as exc:
    HAS_MSRESTAZURE_EXC = exc
    HAS_MSRESTAZURE = False

try:
    from enum import Enum
    from msrestazure.azure_exceptions import CloudError
    from msrestazure import azure_cloud
    from azure.mgmt.network.models import PublicIPAddress, NetworkSecurityGroup, SecurityRule, NetworkInterface, \
        NetworkInterfaceIPConfiguration, Subnet
    from azure.common.credentials import ServicePrincipalCredentials, UserPassCredentials
    from azure.mgmt.network.version import VERSION as network_client_version
    from azure.mgmt.storage.version import VERSION as storage_client_version
    from azure.mgmt.compute.version import VERSION as compute_client_version
    from azure.mgmt.resource.version import VERSION as resource_client_version
    from azure.mgmt.dns.version import VERSION as dns_client_version
    from azure.mgmt.web.version import VERSION as web_client_version
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource.resources import ResourceManagementClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.dns import DnsManagementClient
    from azure.mgmt.web import WebSiteManagementClient
    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.storage.cloudstorageaccount import CloudStorageAccount
except ImportError as exc:
    HAS_AZURE_EXC = exc
    HAS_AZURE = False


try:
    from azure.cli.core.util import CLIError
    from azure.common.credentials import get_azure_cli_credentials, get_cli_profile
    from azure.common.cloud import get_cli_active_cloud
except ImportError:
    HAS_AZURE_CLI_CORE = False


def azure_id_to_dict(id):
    pieces = re.sub(r'^\/', '', id).split('/')
    result = {}
    index = 0
    while index < len(pieces) - 1:
        result[pieces[index]] = pieces[index + 1]
        index += 1
    return result


AZURE_PKG_VERSIONS = {
    StorageManagementClient.__name__: {
        'package_name': 'storage',
        'expected_version': '1.0.0',
        'installed_version': storage_client_version
    },
    ComputeManagementClient.__name__: {
        'package_name': 'compute',
        'expected_version': '1.0.0',
        'installed_version': compute_client_version
    },
    NetworkManagementClient.__name__: {
        'package_name': 'network',
        'expected_version': '1.0.0',
        'installed_version': network_client_version
    },
    ResourceManagementClient.__name__: {
        'package_name': 'resource',
        'expected_version': '1.1.0',
        'installed_version': resource_client_version
    },
    DnsManagementClient.__name__: {
        'package_name': 'dns',
        'expected_version': '1.0.1',
        'installed_version': dns_client_version
    },
    WebSiteManagementClient.__name__: {
        'package_name': 'web',
        'expected_version': '0.32.0',
        'installed_version': web_client_version
    },
} if HAS_AZURE else {}


AZURE_MIN_RELEASE = '2.0.0'


class AzureRMModuleBase(object):
    def __init__(self, derived_arg_spec, bypass_checks=False, no_log=False,
                 check_invalid_arguments=True, mutually_exclusive=None, required_together=None,
                 required_one_of=None, add_file_common_args=False, supports_check_mode=False,
                 required_if=None, supports_tags=True, facts_module=False, skip_exec=False):

        merged_arg_spec = dict()
        merged_arg_spec.update(AZURE_COMMON_ARGS)
        if supports_tags:
            merged_arg_spec.update(AZURE_TAG_ARGS)

        if derived_arg_spec:
            merged_arg_spec.update(derived_arg_spec)

        merged_required_if = list(AZURE_COMMON_REQUIRED_IF)
        if required_if:
            merged_required_if += required_if

        self.module = AnsibleModule(argument_spec=merged_arg_spec,
                                    bypass_checks=bypass_checks,
                                    no_log=no_log,
                                    check_invalid_arguments=check_invalid_arguments,
                                    mutually_exclusive=mutually_exclusive,
                                    required_together=required_together,
                                    required_one_of=required_one_of,
                                    add_file_common_args=add_file_common_args,
                                    supports_check_mode=supports_check_mode,
                                    required_if=merged_required_if)

        if not HAS_PACKAGING_VERSION:
            self.fail("Do you have packaging installed? Try `pip install packaging`"
                      "- {0}".format(HAS_PACKAGING_VERSION_EXC))

        if not HAS_MSRESTAZURE:
            self.fail("Do you have msrestazure installed? Try `pip install msrestazure`"
                      "- {0}".format(HAS_MSRESTAZURE_EXC))

        if not HAS_AZURE:
            self.fail("Do you have azure>={1} installed? Try `pip install 'azure>={1}' --upgrade`"
                      "- {0}".format(HAS_AZURE_EXC, AZURE_MIN_RELEASE))

        self._cloud_environment = None
        self._network_client = None
        self._storage_client = None
        self._resource_client = None
        self._compute_client = None
        self._dns_client = None
        self._web_client = None
        self._containerservice_client = None

        self.check_mode = self.module.check_mode
        self.facts_module = facts_module
        # self.debug = self.module.params.get('debug')

        # authenticate
        self.credentials = self._get_credentials(self.module.params)
        if not self.credentials:
            self.fail("Failed to get credentials. Either pass as parameters, set environment variables, "
                      "or define a profile in ~/.azure/credentials or be logged using AzureCLI.")

        # if cloud_environment specified, look up/build Cloud object
        raw_cloud_env = self.credentials.get('cloud_environment')
        if not raw_cloud_env:
            self._cloud_environment = azure_cloud.AZURE_PUBLIC_CLOUD  # SDK default
        else:
            # try to look up "well-known" values via the name attribute on azure_cloud members
            all_clouds = [x[1] for x in inspect.getmembers(azure_cloud) if isinstance(x[1], azure_cloud.Cloud)]
            matched_clouds = [x for x in all_clouds if x.name == raw_cloud_env]
            if len(matched_clouds) == 1:
                self._cloud_environment = matched_clouds[0]
            elif len(matched_clouds) > 1:
                self.fail("Azure SDK failure: more than one cloud matched for cloud_environment name '{0}'".format(raw_cloud_env))
            else:
                if not urlparse.urlparse(raw_cloud_env).scheme:
                    self.fail("cloud_environment must be an endpoint discovery URL or one of {0}".format([x.name for x in all_clouds]))
                try:
                    self._cloud_environment = azure_cloud.get_cloud_from_metadata_endpoint(raw_cloud_env)
                except Exception as e:
                    self.fail("cloud_environment {0} could not be resolved: {1}".format(raw_cloud_env, e.message), exception=traceback.format_exc(e))

        if self.credentials.get('subscription_id', None) is None:
            self.fail("Credentials did not include a subscription_id value.")
        self.log("setting subscription_id")
        self.subscription_id = self.credentials['subscription_id']

        if self.credentials.get('client_id') is not None and \
           self.credentials.get('secret') is not None and \
           self.credentials.get('tenant') is not None:
            self.azure_credentials = ServicePrincipalCredentials(client_id=self.credentials['client_id'],
                                                                 secret=self.credentials['secret'],
                                                                 tenant=self.credentials['tenant'],
                                                                 cloud_environment=self._cloud_environment)

        elif self.credentials.get('ad_user') is not None and self.credentials.get('password') is not None:
            tenant = self.credentials.get('tenant')
            if not tenant:
                tenant = 'common'  # SDK default

            self.azure_credentials = UserPassCredentials(self.credentials['ad_user'],
                                                         self.credentials['password'],
                                                         tenant=tenant,
                                                         cloud_environment=self._cloud_environment)
        else:
            self.fail("Failed to authenticate with provided credentials. Some attributes were missing. "
                      "Credentials must include client_id, secret and tenant or ad_user and password or "
                      "be logged using AzureCLI.")

        # common parameter validation
        if self.module.params.get('tags'):
            self.validate_tags(self.module.params['tags'])

        if not skip_exec:
            res = self.exec_module(**self.module.params)
            self.module.exit_json(**res)

    def check_client_version(self, client_type):
        # Ensure Azure modules are at least 2.0.0rc5.
        package_version = AZURE_PKG_VERSIONS.get(client_type.__name__, None)
        if package_version is not None:
            client_name = package_version.get('package_name')
            client_version = package_version.get('installed_version')
            expected_version = package_version.get('expected_version')
            if Version(client_version) < Version(expected_version):
                self.fail("Installed {0} client version is {1}. The supported version is {2}. Try "
                          "`pip install azure>={3} --upgrade`".format(client_name, client_version, expected_version,
                                                                      AZURE_MIN_RELEASE))

    def exec_module(self, **kwargs):
        self.fail("Error: {0} failed to implement exec_module method.".format(self.__class__.__name__))

    def fail(self, msg, **kwargs):
        '''
        Shortcut for calling module.fail()

        :param msg: Error message text.
        :param kwargs: Any key=value pairs
        :return: None
        '''
        self.module.fail_json(msg=msg, **kwargs)

    def log(self, msg, pretty_print=False):
        pass
        # Use only during module development
        # if self.debug:
        #     log_file = open('azure_rm.log', 'a')
        #     if pretty_print:
        #         log_file.write(json.dumps(msg, indent=4, sort_keys=True))
        #     else:
        #         log_file.write(msg + u'\n')

    def validate_tags(self, tags):
        '''
        Check if tags dictionary contains string:string pairs.

        :param tags: dictionary of string:string pairs
        :return: None
        '''
        if not self.facts_module:
            if not isinstance(tags, dict):
                self.fail("Tags must be a dictionary of string:string values.")
            for key, value in tags.items():
                if not isinstance(value, str):
                    self.fail("Tags values must be strings. Found {0}:{1}".format(str(key), str(value)))

    def update_tags(self, tags):
        '''
        Call from the module to update metadata tags. Returns tuple
        with bool indicating if there was a change and dict of new
        tags to assign to the object.

        :param tags: metadata tags from the object
        :return: bool, dict
        '''
        new_tags = copy.copy(tags) if isinstance(tags, dict) else dict()
        changed = False
        if isinstance(self.module.params.get('tags'), dict):
            for key, value in self.module.params['tags'].items():
                if not new_tags.get(key) or new_tags[key] != value:
                    changed = True
                    new_tags[key] = value
            if isinstance(tags, dict):
                for key, value in tags.items():
                    if not self.module.params['tags'].get(key):
                        new_tags.pop(key)
                        changed = True
        return changed, new_tags

    def has_tags(self, obj_tags, tag_list):
        '''
        Used in fact modules to compare object tags to list of parameter tags. Return true if list of parameter tags
        exists in object tags.

        :param obj_tags: dictionary of tags from an Azure object.
        :param tag_list: list of tag keys or tag key:value pairs
        :return: bool
        '''

        if not obj_tags and tag_list:
            return False

        if not tag_list:
            return True

        matches = 0
        result = False
        for tag in tag_list:
            tag_key = tag
            tag_value = None
            if ':' in tag:
                tag_key, tag_value = tag.split(':')
            if tag_value and obj_tags.get(tag_key) == tag_value:
                matches += 1
            elif not tag_value and obj_tags.get(tag_key):
                matches += 1
        if matches == len(tag_list):
            result = True
        return result

    def get_resource_group(self, resource_group):
        '''
        Fetch a resource group.

        :param resource_group: name of a resource group
        :return: resource group object
        '''
        try:
            return self.rm_client.resource_groups.get(resource_group)
        except CloudError as cloud_error:
            self.fail("Error retrieving resource group {0} - {1}".format(resource_group, cloud_error.message))
        except Exception as exc:
            self.fail("Error retrieving resource group {0} - {1}".format(resource_group, str(exc)))

    def _get_azure_cli_profile(self):
        if not HAS_AZURE_CLI_CORE:
            self.fail("Do you have azure-cli-core installed? Try `pip install 'azure-cli-core' --upgrade`")
        try:
            credentials, subscription_id = get_azure_cli_credentials()
            self._cloud_environment = get_cli_active_cloud()
            return {
                'credentials': credentials,
                'subscription_id': subscription_id
            }
        except CLIError as err:
            self.fail("AzureCLI profile cannot be loaded - {0}".format(err))

    def _get_profile(self, profile="default"):
        path = expanduser("~/.azure/credentials")
        try:
            config = configparser.ConfigParser()
            config.read(path)
        except Exception as exc:
            self.fail("Failed to access {0}. Check that the file exists and you have read "
                      "access. {1}".format(path, str(exc)))
        credentials = dict()
        for key in AZURE_CREDENTIAL_ENV_MAPPING:
            try:
                credentials[key] = config.get(profile, key, raw=True)
            except:
                pass

        if credentials.get('subscription_id'):
            return credentials

        return None

    def _get_env_credentials(self):
        env_credentials = dict()
        for attribute, env_variable in AZURE_CREDENTIAL_ENV_MAPPING.items():
            env_credentials[attribute] = os.environ.get(env_variable, None)

        if (env_credentials['cli_default_profile'] or '').lower() in ["true", "yes", "1"]:
            return self._get_azure_cli_profile()

        if env_credentials['profile']:
            credentials = self._get_profile(env_credentials['profile'])
            return credentials

        if env_credentials.get('subscription_id') is not None:
            return env_credentials

        return None

    def _get_credentials(self, params):
        # Get authentication credentials.
        # Precedence: module parameters-> environment variables-> default profile in ~/.azure/credentials.

        self.log('Getting credentials')

        arg_credentials = dict()
        for attribute, env_variable in AZURE_CREDENTIAL_ENV_MAPPING.items():
            arg_credentials[attribute] = params.get(attribute, None)

        if arg_credentials['cli_default_profile']:
            self.log('Retrieving credentials from Azure CLI current profile')
            return self._get_azure_cli_profile()

        # try module params
        if arg_credentials['profile'] is not None:
            self.log('Retrieving credentials with profile parameter.')
            credentials = self._get_profile(arg_credentials['profile'])
            return credentials

        if arg_credentials['subscription_id']:
            self.log('Received credentials from parameters.')
            return arg_credentials

        # try environment
        env_credentials = self._get_env_credentials()
        if env_credentials:
            self.log('Received credentials from env.')
            return env_credentials

        # try default profile from ~./azure/credentials
        default_credentials = self._get_profile()
        if default_credentials:
            self.log('Retrieved default profile credentials from ~/.azure/credentials.')
            return default_credentials

        return None

    def serialize_obj(self, obj, class_name, enum_modules=None):
        '''
        Return a JSON representation of an Azure object.

        :param obj: Azure object
        :param class_name: Name of the object's class
        :param enum_modules: List of module names to build enum dependencies from.
        :return: serialized result
        '''
        enum_modules = [] if enum_modules is None else enum_modules

        dependencies = dict()
        if enum_modules:
            for module_name in enum_modules:
                mod = importlib.import_module(module_name)
                for mod_class_name, mod_class_obj in inspect.getmembers(mod, predicate=inspect.isclass):
                    dependencies[mod_class_name] = mod_class_obj
            self.log("dependencies: ")
            self.log(str(dependencies))
        serializer = Serializer(classes=dependencies)
        return serializer.body(obj, class_name, keep_readonly=True)

    def get_poller_result(self, poller, wait=5):
        '''
        Consistent method of waiting on and retrieving results from Azure's long poller

        :param poller Azure poller object
        :return object resulting from the original request
        '''
        try:
            delay = wait
            while not poller.done():
                self.log("Waiting for {0} sec".format(delay))
                poller.wait(timeout=delay)
            return poller.result()
        except Exception as exc:
            self.log(str(exc))
            raise

    def check_provisioning_state(self, azure_object, requested_state='present'):
        '''
        Check an Azure object's provisioning state. If something did not complete the provisioning
        process, then we cannot operate on it.

        :param azure_object An object such as a subnet, storageaccount, etc. Must have provisioning_state
                            and name attributes.
        :return None
        '''

        if hasattr(azure_object, 'properties') and hasattr(azure_object.properties, 'provisioning_state') and \
           hasattr(azure_object, 'name'):
            # resource group object fits this model
            if isinstance(azure_object.properties.provisioning_state, Enum):
                if azure_object.properties.provisioning_state.value != AZURE_SUCCESS_STATE and \
                   requested_state != 'absent':
                    self.fail("Error {0} has a provisioning state of {1}. Expecting state to be {2}.".format(
                              azure_object.name, azure_object.properties.provisioning_state, AZURE_SUCCESS_STATE))
                return
            if azure_object.properties.provisioning_state != AZURE_SUCCESS_STATE and \
               requested_state != 'absent':
                self.fail("Error {0} has a provisioning state of {1}. Expecting state to be {2}.".format(
                    azure_object.name, azure_object.properties.provisioning_state, AZURE_SUCCESS_STATE))
            return

        if hasattr(azure_object, 'provisioning_state') or not hasattr(azure_object, 'name'):
            if isinstance(azure_object.provisioning_state, Enum):
                if azure_object.provisioning_state.value != AZURE_SUCCESS_STATE and requested_state != 'absent':
                    self.fail("Error {0} has a provisioning state of {1}. Expecting state to be {2}.".format(
                        azure_object.name, azure_object.provisioning_state, AZURE_SUCCESS_STATE))
                return
            if azure_object.provisioning_state != AZURE_SUCCESS_STATE and requested_state != 'absent':
                self.fail("Error {0} has a provisioning state of {1}. Expecting state to be {2}.".format(
                    azure_object.name, azure_object.provisioning_state, AZURE_SUCCESS_STATE))

    def get_blob_client(self, resource_group_name, storage_account_name, storage_blob_type='block'):
        keys = dict()
        try:
            # Get keys from the storage account
            self.log('Getting keys')
            account_keys = self.storage_client.storage_accounts.list_keys(resource_group_name, storage_account_name)
        except Exception as exc:
            self.fail("Error getting keys for account {0} - {1}".format(storage_account_name, str(exc)))

        try:
            self.log('Create blob service')
            if storage_blob_type == 'page':
                return CloudStorageAccount(storage_account_name, account_keys.keys[0].value).create_page_blob_service()
            elif storage_blob_type == 'block':
                return CloudStorageAccount(storage_account_name, account_keys.keys[0].value).create_block_blob_service()
            else:
                raise Exception("Invalid storage blob type defined.")
        except Exception as exc:
            self.fail("Error creating blob service client for storage account {0} - {1}".format(storage_account_name,
                                                                                                str(exc)))

    def create_default_pip(self, resource_group, location, name, allocation_method='Dynamic'):
        '''
        Create a default public IP address <name>01 to associate with a network interface.
        If a PIP address matching <vm name>01 exists, return it. Otherwise, create one.

        :param resource_group: name of an existing resource group
        :param location: a valid azure location
        :param name: base name to assign the public IP address
        :param allocation_method: one of 'Static' or 'Dynamic'
        :return: PIP object
        '''
        public_ip_name = name + '01'
        pip = None

        self.log("Starting create_default_pip {0}".format(public_ip_name))
        self.log("Check to see if public IP {0} exists".format(public_ip_name))
        try:
            pip = self.network_client.public_ip_addresses.get(resource_group, public_ip_name)
        except CloudError:
            pass

        if pip:
            self.log("Public ip {0} found.".format(public_ip_name))
            self.check_provisioning_state(pip)
            return pip

        params = PublicIPAddress(
            location=location,
            public_ip_allocation_method=allocation_method,
        )
        self.log('Creating default public IP {0}'.format(public_ip_name))
        try:
            poller = self.network_client.public_ip_addresses.create_or_update(resource_group, public_ip_name, params)
        except Exception as exc:
            self.fail("Error creating {0} - {1}".format(public_ip_name, str(exc)))

        return self.get_poller_result(poller)

    def create_default_securitygroup(self, resource_group, location, name, os_type, open_ports):
        '''
        Create a default security group <name>01 to associate with a network interface. If a security group matching
        <name>01 exists, return it. Otherwise, create one.

        :param resource_group: Resource group name
        :param location: azure location name
        :param name: base name to use for the security group
        :param os_type: one of 'Windows' or 'Linux'. Determins any default rules added to the security group.
        :param ssh_port: for os_type 'Linux' port used in rule allowing SSH access.
        :param rdp_port: for os_type 'Windows' port used in rule allowing RDP access.
        :return: security_group object
        '''
        security_group_name = name + '01'
        group = None

        self.log("Create security group {0}".format(security_group_name))
        self.log("Check to see if security group {0} exists".format(security_group_name))
        try:
            group = self.network_client.network_security_groups.get(resource_group, security_group_name)
        except CloudError:
            pass

        if group:
            self.log("Security group {0} found.".format(security_group_name))
            self.check_provisioning_state(group)
            return group

        parameters = NetworkSecurityGroup()
        parameters.location = location

        if not open_ports:
            # Open default ports based on OS type
            if os_type == 'Linux':
                # add an inbound SSH rule
                parameters.security_rules = [
                    SecurityRule('Tcp', '*', '*', 'Allow', 'Inbound', description='Allow SSH Access',
                                 source_port_range='*', destination_port_range='22', priority=100, name='SSH')
                ]
                parameters.location = location
            else:
                # for windows add inbound RDP and WinRM rules
                parameters.security_rules = [
                    SecurityRule('Tcp', '*', '*', 'Allow', 'Inbound', description='Allow RDP port 3389',
                                 source_port_range='*', destination_port_range='3389', priority=100, name='RDP01'),
                    SecurityRule('Tcp', '*', '*', 'Allow', 'Inbound', description='Allow WinRM HTTPS port 5986',
                                 source_port_range='*', destination_port_range='5986', priority=101, name='WinRM01'),
                ]
        else:
            # Open custom ports
            parameters.security_rules = []
            priority = 100
            for port in open_ports:
                priority += 1
                rule_name = "Rule_{0}".format(priority)
                parameters.security_rules.append(
                    SecurityRule('Tcp', '*', '*', 'Allow', 'Inbound', source_port_range='*',
                                 destination_port_range=str(port), priority=priority, name=rule_name)
                )

        self.log('Creating default security group {0}'.format(security_group_name))
        try:
            poller = self.network_client.network_security_groups.create_or_update(resource_group,
                                                                                  security_group_name,
                                                                                  parameters)
        except Exception as exc:
            self.fail("Error creating default security rule {0} - {1}".format(security_group_name, str(exc)))

        return self.get_poller_result(poller)

    def get_mgmt_svc_client(self, client_type, base_url=None, api_version=None):
        self.log('Getting management service client {0}'.format(client_type.__name__))
        self.check_client_version(client_type)
        if api_version:
            client = client_type(self.azure_credentials,
                                 self.subscription_id,
                                 api_version=api_version,
                                 base_url=base_url)
        else:
            client = client_type(self.azure_credentials,
                                 self.subscription_id,
                                 base_url=base_url)

        # Add user agent for Ansible
        client.config.add_user_agent(ANSIBLE_USER_AGENT)
        # Add user agent when running from Cloud Shell
        if CLOUDSHELL_USER_AGENT_KEY in os.environ:
            client.config.add_user_agent(os.environ[CLOUDSHELL_USER_AGENT_KEY])
        # Add user agent when running from VSCode extension
        if VSCODEEXT_USER_AGENT_KEY in os.environ:
            client.config.add_user_agent(os.environ[VSCODEEXT_USER_AGENT_KEY])

        return client

    @property
    def storage_client(self):
        self.log('Getting storage client...')
        if not self._storage_client:
            self._storage_client = self.get_mgmt_svc_client(StorageManagementClient,
                                                            base_url=self._cloud_environment.endpoints.resource_manager,
                                                            api_version='2017-06-01')
        return self._storage_client

    @property
    def network_client(self):
        self.log('Getting network client')
        if not self._network_client:
            self._network_client = self.get_mgmt_svc_client(NetworkManagementClient,
                                                            base_url=self._cloud_environment.endpoints.resource_manager,
                                                            api_version='2017-06-01')
        return self._network_client

    @property
    def rm_client(self):
        self.log('Getting resource manager client')
        if not self._resource_client:
            self._resource_client = self.get_mgmt_svc_client(ResourceManagementClient,
                                                             base_url=self._cloud_environment.endpoints.resource_manager,
                                                             api_version='2017-05-10')
        return self._resource_client

    @property
    def compute_client(self):
        self.log('Getting compute client')
        if not self._compute_client:
            self._compute_client = self.get_mgmt_svc_client(ComputeManagementClient,
                                                            base_url=self._cloud_environment.endpoints.resource_manager,
                                                            api_version='2017-03-30')
        return self._compute_client

    @property
    def dns_client(self):
        self.log('Getting dns client')
        if not self._dns_client:
            self._dns_client = self.get_mgmt_svc_client(DnsManagementClient,
                                                        base_url=self._cloud_environment.endpoints.resource_manager)
        return self._dns_client

    @property
    def web_client(self):
        self.log('Getting web client')
        if not self._web_client:
            self._web_client = self.get_mgmt_svc_client(WebSiteManagementClient,
                                                        base_url=self._cloud_environment.endpoints.resource_manager)
        return self._web_client

    @property
    def containerservice_client(self):
        self.log('Getting container service client')
        if not self._containerservice_client:
            self._containerservice_client = self.get_mgmt_svc_client(ContainerServiceClient,
                                                                     base_url=self._cloud_environment.endpoints.resource_manager)
        return self._containerservice_client
