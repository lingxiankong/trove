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
from trove.guestagent.datastore.mysql_common import service


class MySqlAppStatus(service.BaseMySqlAppStatus):
    def __init__(self, docker_client):
        super(MySqlAppStatus, self).__init__(docker_client)


class MySqlApp(service.BaseMySqlApp):
    def __init__(self, status, docker_client):
        super(MySqlApp, self).__init__(status, docker_client)


class MySqlRootAccess(service.BaseMySqlRootAccess):
    def __init__(self, app):
        super(MySqlRootAccess, self).__init__(app)


class MySqlAdmin(service.BaseMySqlAdmin):
    def __init__(self, app):
        root_access = MySqlRootAccess(app)
        super(MySqlAdmin, self).__init__(root_access, app)
