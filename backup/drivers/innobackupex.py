# Copyright 2020 Catalyst Cloud
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import os
import re
import shutil

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging

from backup.drivers import base

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class InnoBackupEx(base.BaseRunner):
    """Implementation of Backup and Restore for InnoBackupEx."""

    restore_cmd = ('xbstream -x -C %(restore_location)s --parallel=2'
                   ' 2>/tmp/xbstream_extract.log')
    prepare_cmd = ('innobackupex'
                   ' --defaults-file=%(restore_location)s/backup-my.cnf'
                   ' --ibbackup=xtrabackup'
                   ' --apply-log'
                   ' %(restore_location)s'
                   ' 2>/tmp/prepare.log')

    @property
    def user_and_pass(self):
        return ('--user=%(user)s --password=%(password)s --host=%(host)s' %
                {'user': CONF.db_user,
                 'password': CONF.db_password,
                 'host': CONF.db_host})

    @property
    def cmd(self):
        cmd = ('innobackupex'
               ' --stream=xbstream'
               ' --parallel=2 ' +
               self.user_and_pass + ' %s' % self.default_data_dir +
               ' 2>/tmp/innobackupex.log'
               )
        return cmd + self.zip_cmd + self.encrypt_cmd

    @property
    def filename(self):
        return '%s.xbstream' % self.base_filename

    def check_process(self):
        """Check the output from innobackupex for 'completed OK!'."""
        LOG.debug('Checking innobackupex process output.')
        with open('/tmp/innobackupex.log', 'r') as backup_log:
            output = backup_log.read()
            if not output:
                LOG.error("Innobackupex log file empty.")
                return False

            last_line = output.splitlines()[-1].strip()
            if not re.search('completed OK!', last_line):
                LOG.error("Innobackupex did not complete successfully.")
                return False

        return True

    def get_metadata(self):
        LOG.debug('Getting metadata for backup %s', self.base_filename)
        meta = {}
        lsn = re.compile(r"The latest check point \(for incremental\): "
                         r"'(\d+)'")
        with open('/tmp/innobackupex.log', 'r') as backup_log:
            output = backup_log.read()
            match = lsn.search(output)
            if match:
                meta = {'lsn': match.group(1)}

        LOG.info("Updated metadata for backup %s: %s", self.base_filename,
                 meta)

        return meta

    def check_restore_process(self):
        """Check whether xbstream restore is successful."""
        LOG.info('Checking return code of xbstream restore process.')
        return_code = self.process.wait()
        if return_code != 0:
            LOG.error('xbstream exited with %s', return_code)
            return False

        with open('/tmp/xbstream_extract.log', 'r') as xbstream_log:
            for line in xbstream_log:
                # Ignore empty lines
                if not line.strip():
                    continue

                LOG.error('xbstream restore failed with: %s',
                          line.rstrip('\n'))
                return False

        return True

    def post_restore(self):
        """Hook that is called after the restore command."""
        LOG.info("Running innobackupex prepare: %s.", self.prepare_command)
        processutils.execute(self.prepare_command, shell=True)

        LOG.info("Checking innobackupex prepare log")
        with open('/tmp/prepare.log', 'r') as prepare_log:
            output = prepare_log.read()
            if not output:
                msg = "innobackupex prepare log file empty"
                raise Exception(msg)

            last_line = output.splitlines()[-1].strip()
            if not re.search('completed OK!', last_line):
                msg = "innobackupex prepare did not complete successfully"
                raise Exception(msg)


class InnoBackupExIncremental(InnoBackupEx):
    """InnoBackupEx incremental backup."""

    incremental_prep = ('innobackupex'
                        ' --defaults-file=%(restore_location)s/backup-my.cnf'
                        ' --ibbackup=xtrabackup'
                        ' --apply-log'
                        ' --redo-only'
                        ' %(restore_location)s'
                        ' %(incremental_args)s'
                        ' 2>/tmp/innoprepare.log')

    def __init__(self, *args, **kwargs):
        if not kwargs.get('lsn'):
            raise AttributeError('lsn attribute missing')
        self.parent_location = kwargs.pop('parent_location', '')
        self.parent_checksum = kwargs.pop('parent_checksum', '')
        self.restore_content_length = 0

        super(InnoBackupExIncremental, self).__init__(*args, **kwargs)

    @property
    def cmd(self):
        cmd = ('innobackupex'
               ' --stream=xbstream'
               ' --incremental'
               ' --incremental-lsn=%(lsn)s ' +
               self.user_and_pass + ' %s' % self.default_data_dir +
               ' 2>/tmp/innobackupex.log')
        return cmd + self.zip_cmd + self.encrypt_cmd

    def get_metadata(self):
        _meta = super(InnoBackupExIncremental, self).get_metadata()
        _meta.update({
            'parent_location': self.parent_location,
            'parent_checksum': self.parent_checksum,
        })
        return _meta

    def _incremental_restore_cmd(self, incremental_dir):
        """Return a command for a restore with a incremental location."""
        args = {'restore_location': incremental_dir}
        return (self.decrypt_cmd + self.unzip_cmd + self.restore_cmd % args)

    def _incremental_prepare_cmd(self, incremental_dir):
        if incremental_dir is not None:
            incremental_arg = '--incremental-dir=%s' % incremental_dir
        else:
            incremental_arg = ''

        args = {
            'restore_location': self.restore_location,
            'incremental_args': incremental_arg,
        }

        return self.incremental_prep % args

    def _incremental_prepare(self, incremental_dir):
        prepare_cmd = self._incremental_prepare_cmd(incremental_dir)

        LOG.info("Running innobackupex restore prepare command: %s.",
                 prepare_cmd)
        processutils.execute(prepare_cmd, shell=True)

    def _incremental_restore(self, location, checksum):
        """Recursively apply backups from all parents.

        If we are the parent then we restore to the restore_location and
        we apply the logs to the restore_location only.

        Otherwise if we are an incremental we restore to a subfolder to
        prevent stomping on the full restore data. Then we run apply log
        with the '--incremental-dir' flag

        :param location: The source backup location.
        :param checksum: Checksum of the source backup for validation.
        """
        metadata = self.storage.load_metadata(location, checksum)
        incremental_dir = None

        if 'parent_location' in metadata:
            LOG.info("Restoring parent: %(parent_location)s"
                     " checksum: %(parent_checksum)s.", metadata)

            parent_location = metadata['parent_location']
            parent_checksum = metadata['parent_checksum']
            # Restore parents recursively so backup are applied sequentially
            self._incremental_restore(parent_location, parent_checksum)
            # for *this* backup set the incremental_dir
            # just use the checksum for the incremental path as it is
            # sufficiently unique /var/lib/mysql/<checksum>
            incremental_dir = os.path.join('/var/lib/mysql', checksum)
            os.makedirs(incremental_dir)
            command = self._incremental_restore_cmd(incremental_dir)
        else:
            # The parent (full backup) use the same command from InnobackupEx
            # super class and do not set an incremental_dir.
            command = self.restore_command

        self.restore_content_length += self.unpack(location, checksum, command)
        self._incremental_prepare(incremental_dir)

        # Delete after restoring this part of backup
        if incremental_dir:
            shutil.rmtree(incremental_dir)

    def run_restore(self):
        """Run incremental restore.

        First grab all parents and prepare them with '--redo-only'. After
        all backups are restored the super class InnoBackupEx post_restore
        method is called to do the final prepare with '--apply-log'
        """
        self._incremental_restore(self.location, self.checksum)
        return self.restore_content_length
