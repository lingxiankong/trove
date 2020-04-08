# Copyright 2013 OpenStack Foundation
# Copyright 2013 Rackspace Hosting
# Copyright 2013 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#

from oslo_log import log as logging

from trove.common import cfg
from trove.common import exception
from trove.common import instance as rd_instance
from trove.common.notification import EndNotification
from trove.guestagent import backup
from trove.guestagent import volume
from trove.guestagent.common import operating_system
from trove.guestagent.datastore import manager
from trove.guestagent.utils import mysql as mysql_util

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class MySqlManager(manager.Manager):
    def __init__(self, mysql_app, mysql_app_status, mysql_admin,
                 manager_name='mysql'):
        super(MySqlManager, self).__init__(manager_name)

        self.app = mysql_app
        self.status = mysql_app_status
        self.adm = mysql_admin
        self.volume_do_not_start_on_reboot = False

    def get_service_status(self):
        try:
            with mysql_util.SqlClient(self.app.get_engine()) as client:
                cmd = "SELECT 1;"
                client.execute(cmd)

            LOG.debug("Database service check: database query is responsive")
            return rd_instance.ServiceStatuses.HEALTHY
        except Exception:
            return super(MySqlManager, self).get_service_status()

    def create_database(self, context, databases):
        with EndNotification(context):
            return self.adm.create_database(databases)

    def create_user(self, context, users):
        with EndNotification(context):
            self.adm.create_user(users)

    def enable_root_with_password(self, context, root_password=None):
        return self.adm.enable_root(root_password)

    def is_root_enabled(self, context):
        return self.adm.is_root_enabled()

    def perform_restore(self, context, restore_location, backup_info):
        LOG.info("Restoring database from backup %s to %s, backup_info: %s",
                 backup_info['id'], restore_location, backup_info)
        try:
            backup.restore(context, backup_info, restore_location)
        except Exception:
            LOG.exception("Error performing restore from backup %s.",
                          backup_info['id'])
            self.status.set_status(rd_instance.ServiceStatuses.FAILED)
            raise
        LOG.info("Restored database successfully.")

    def do_prepare(self, context, packages, databases, memory_mb, users,
                   device_path, mount_point, backup_info,
                   config_contents, root_password, overrides,
                   cluster_config, snapshot):
        """This is called from prepare in the base class."""
        if device_path:
            LOG.info('Preparing the storage for %s, mount path %s',
                     device_path, mount_point)

            self.app.stop_db(
                do_not_start_on_reboot=self.volume_do_not_start_on_reboot
            )

            device = volume.VolumeDevice(device_path)
            # unmount if device is already mounted
            device.unmount_device(device_path)
            device.format()
            if operating_system.list_files_in_directory(mount_point):
                # rsync existing data to a "data" sub-directory
                # on the new volume
                device.migrate_data(mount_point, target_subdir="data")
            # mount the volume
            device.mount(mount_point)
            operating_system.remove("%s/lost+found" % mount_point, force=True)
            operating_system.chown(mount_point, CONF.database_service_uid,
                                   CONF.database_service_uid,
                                   recursive=False, as_root=True)

            # We need to temporarily update the default my.cnf so that
            # mysql will start after the volume is mounted. Later on it
            # will be changed based on the config template
            # (see MySqlApp.secure()) and restart.
            self.app.set_data_dir(mount_point + '/data')

        LOG.info('Preparing database configuration')
        self.app.configuration_manager.save_configuration(config_contents)
        self.app.update_overrides(overrides)

        # Restore from backup
        if backup_info:
            self.perform_restore(context, mount_point + "/data", backup_info)

        self.app.start_db()
        self.app.secure()

        enable_remote_root = (backup_info and self.adm.is_root_enabled())
        if enable_remote_root:
            self.status.report_root(context)
        else:
            self.app.secure_root()

        if snapshot:
            # This instance is a replication slave
            self.attach_replica(context, snapshot, snapshot['config'])

    def _validate_slave_for_replication(self, context, replica_info):
        if replica_info['replication_strategy'] != self.replication_strategy:
            raise exception.IncompatibleReplicationStrategy(
                replica_info.update({
                    'guest_strategy': self.replication_strategy
                }))

        volume_stats = self.get_filesystem_stats(context, None)
        if (volume_stats.get('total', 0.0) <
            replica_info['dataset']['dataset_size']):
            raise exception.InsufficientSpaceForReplica(
                replica_info.update({
                    'slave_volume_size': volume_stats.get('total', 0.0)
                }))

    def attach_replica(self, context, replica_info, slave_config):
        LOG.info("Attaching replica.")
        try:
            if 'replication_strategy' in replica_info:
                self._validate_slave_for_replication(context, replica_info)
            self.replication.enable_as_slave(self.app, replica_info,
                                             slave_config)
        except Exception:
            LOG.exception("Error enabling replication.")
            self.status.set_status(rd_instance.ServiceStatuses.FAILED)
            raise

    def stop_db(self, context, do_not_start_on_reboot=False):
        self.app.stop_db(do_not_start_on_reboot=do_not_start_on_reboot)

    def restart(self, context):
        self.app.restart()
