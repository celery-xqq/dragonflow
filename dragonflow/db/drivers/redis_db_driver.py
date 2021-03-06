#    Licensed under the Apache License, Version 2.0 (the "License"); you may
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

import crc16
from oslo_log import log
import re
from redis import client as redis_client
from redis import exceptions
import six

from dragonflow.common import exceptions as df_exceptions
from dragonflow import conf as cfg
from dragonflow.db import db_api
from dragonflow.db import db_common

LOG = log.getLogger(__name__)

REDIS_NSLOTS = 16384


def key2slot(key):
    k = six.text_type(key)
    start = k.find('{')
    if start > -1:
        end = k.find('}', start + 1)
        if end > -1 and end != start + 1:
            k = k[start + 1:end]
    return crc16.crc16xmodem(k.encode('utf-8')) % REDIS_NSLOTS


class Node(object):
    def __init__(self, ip, port, node_id=None):
        self.ip = ip
        self.port = port
        self.node_id = node_id
        self._client = None

    @property
    def client(self):
        if self._client is None:
            decode = not six.PY2
            self._client = redis_client.StrictRedis(host=self.ip,
                                                    port=self.port,
                                                    decode_responses=decode)
        return self._client

    @property
    def key(self):
        return (self.ip, self.port)


class Cluster(object):
    def __init__(self, nodes):
        self._is_cluster = True
        self._configured_nodes = (Node(*node) for node in nodes)
        self._nodes_by_host = {}
        self._nodes_by_slot = [None] * REDIS_NSLOTS
        self._covered = False

    def get_node(self, key):
        if self._is_cluster:
            return self._nodes_by_slot[key2slot(key)]
        else:
            return self._nodes_by_host

    def get_node_by_host(self, ip, port):
        if self._is_cluster:
            return self._nodes_by_host[(ip, port)]
        else:
            return self._nodes_by_host

    def is_cluster_covered(self):
        try:
            self._nodes_by_slot.index(None)
        except ValueError:
            return True
        else:
            return False

    def populate_cluster(self):
        for node in self._configured_nodes:
            client = node.client
            try:
                slots = client.execute_command('CLUSTER', 'SLOTS')
            except exceptions.ConnectionError:
                LOG.exception('Error connecting to cluster node %s:%s',
                              node.ip, node.port)
                continue
            except exceptions.ResponseError as e:
                if str(e).find('cluster support disabled') != -1:
                    LOG.info('Using a single non-cluster node %s:%s',
                             node.ip, node.port)
                    self._nodes_by_host = node
                    self._is_cluster = False
                    return
                LOG.exception('Response error from node %s:%s')
                continue
            self._is_cluster = True
            for slot_info in slots:
                (range_begin, range_end, master_info) = slot_info[0:3]
                master = Node(*master_info)
                self._nodes_by_host[master.key] = master
                for slot in range(int(range_begin), int(range_end) + 1):
                    self._nodes_by_slot[slot] = master
            if self.is_cluster_covered():
                self._covered = True
                break
        if not self._covered:
            LOG.error('Redis cluster not covering slot space')
        for node in self._nodes_by_host.values():
            LOG.info('Cluster node: %s:%s', node.ip, node.port)

    @property
    def nodes(self):
        if self._is_cluster:
            return self._nodes_by_host.values()
        else:
            return (self._nodes_by_host, )


class RedisDbDriver(db_api.DbApi):
    def __init__(self, *args, **kwargs):
        super(RedisDbDriver, self).__init__(*args, **kwargs)
        self._table_strip_re = re.compile('^{.+}(.+)$')
        self.config = cfg.CONF.df_redis
        self.BATCH_KEY_AMOUNT = self.config.batch_amount
        self.RETRY_COUNT = self.config.retries

    def initialize(self, db_ip, db_port, **args):
        nodes = self._config_to_nodes(args['config'].remote_db_hosts)
        self._cluster = Cluster(nodes)
        self._cluster.populate_cluster()

    @staticmethod
    def _config_to_nodes(hosts_list):
        def host_to_node(host):
            (ip, port) = host.split(':')
            return (ip, int(port))

        return map(host_to_node, hosts_list)

    @staticmethod
    def _key_name(table, topic, key):
        return '{%s.%s}%s' % (table, topic or '', key)

    def _key_command(self, command, key, *args):
        node = self._cluster.get_node(key)
        ask = False
        retry = 0
        command_pcs = [command, key]
        command_pcs.extend(args)
        while retry < self.RETRY_COUNT:
            LOG.debug('Executing command "%s" (retry %s)', command_pcs, retry)
            if node is None:
                LOG.error('Error finding node for key %s in cluster', key)
                self._cluster.populate_cluster()
            try:
                if ask:
                    node.client.execute_command('ASKING')
                    ask = False
                return node.client.execute_command(*command_pcs)
            except exceptions.ResponseError as e:
                (reason, slot, ip_port) = str(e).split(' ')
                (ip, port) = ip_port.split(':')
                if reason == 'MOVED':
                    self._cluster.populate_cluster()
                    node = self._cluster.get_node(key)
                if reason == 'ASK':
                    node = self._cluster.get_node_by_host(ip, port)
                    ask = True
            except exceptions.ConnectionError as e:
                LOG.exception('Connection to node %s:%s failed, refreshing',
                              node.ip, node.port)
                self._cluster.populate_cluster()
                node = self._cluster.get_node(key)
            retry += 1

        raise df_exceptions.DBKeyNotFound(key=key)

    def create_table(self, table):
        pass

    def delete_table(self, table):
        self._bulk_operation(table, None, 'DEL')

    def _get_key_topic(self, table, key, topic):
        real_key = self._key_name(table, topic, key)
        value = self._key_command('GET', real_key)
        if value is None:
            raise df_exceptions.DBKeyNotFound(key=key)
        return value

    def _get_key_notopic(self, table, key):
        result = []

        def add_key(k, v):
            result.append(v)

        self._bulk_operation(table, None, 'GET', key_pattern=key,
                             entry_cb=add_key)
        n_keys = len(result)
        if n_keys != 1:
            LOG.error('Found %d entries with key "%s"', n_keys, key)
            raise df_exceptions.DBKeyNotFound(key=key)

        return result[0]

    def get_key(self, table, key, topic=None):
        if topic is None:
            return self._get_key_notopic(table, key)
        else:
            return self._get_key_topic(table, key, topic)

    def set_key(self, table, key, value, topic=None):
        if topic is None:
            real_key = self._key_name_infer_topic(table, key)
        else:
            real_key = self._key_name(table, topic, key)
        self._key_command('SET', real_key, value)

    def create_key(self, table, key, value, topic=None):
        real_key = self._key_name(table, topic, key)
        self._key_command('SET', real_key, value)

    def delete_key(self, table, key, topic=None):
        if topic is None:
            real_key = self._key_name_infer_topic(table, key)
        else:
            real_key = self._key_name(table, topic, key)
        self._key_command('DEL', real_key)

    def _bulk_execute(self, node, keys, command, args=()):
        pipeline = node.client.pipeline(transaction=False)
        retry = 0
        command_pcs = [command, None]
        command_pcs.extend(args)
        while retry < self.RETRY_COUNT:
            for key in keys:
                command_pcs[1] = key
                pipeline.execute_command(*command_pcs)
            try:
                values = pipeline.execute(raise_on_error=False)
                return zip(keys, values)
            except exceptions.RedisError:
                LOG.exception('Error executing pipeline at retry %d', retry)
                retry += 1
        return False

    def _bulk_operation(self, table, topic, command, args=(), key_pattern=None,
                        entry_cb=None, stop_on_fail=False):
        def is_error(value):
            return isinstance(value, exceptions.RedisError)

        (pattern, nodes) = self._query_info(table, topic, key_pattern)
        success = True
        batch_key_amount = self.BATCH_KEY_AMOUNT
        LOG.debug('Performing bulk operation "%s" on table %s topic %s',
                  command, table, topic or 'None')
        for node in nodes:
            node_failed_keys = set()
            retry = 0
            while retry < self.RETRY_COUNT:
                try:
                    node_keys = list(self._get_all_keys_from_node(node,
                                                                  pattern))
                    break
                except exceptions.RedisError:
                    LOG.exception('Error get keys from node %s:%s retry %d',
                                  node.ip, node.port, retry)
                    retry += 1
            LOG.debug('Node %s:%s has %d keys for table %s topic %s',
                      node.ip, node.port, len(node_keys), table,
                      topic or 'None')
            if retry == self.RETRY_COUNT:
                raise df_exceptions.DBKeyNotFound('ALL KEYS')
            bulk_begin = 0
            bulk_end = batch_key_amount
            while bulk_begin < len(node_keys):
                LOG.debug('Working on chunk %d:%d', bulk_begin, bulk_end)
                result = self._bulk_execute(
                    node, node_keys[bulk_begin:bulk_end], command, args)
                if result is False:
                    LOG.error('Error executing bulk operation on node %s:%s',
                              node.ip, node.port)
                    if stop_on_fail:
                        return False
                    else:
                        continue
                for (k, v) in result:
                    if is_error(v):
                        LOG.warning('Bulk operation error node %s:%s key "%s"',
                                    node.ip, node.port, k)
                        if stop_on_fail:
                            return False
                        node_failed_keys.update(k)
                    elif v is not None and callable(entry_cb):
                        entry_cb(k, v)
                bulk_begin += batch_key_amount
                bulk_end += batch_key_amount

            for key in node_failed_keys:
                try:
                    value = self._key_command(command, key, args)
                except Exception:
                    LOG.warning('Failed to process key "%s" from node %s:%s',
                                key, node.ip, node.port)
                    if stop_on_fail:
                        return False
                    success = False
                else:
                    if callable(entry_cb):
                        entry_cb(key, value)
        return success

    def get_all_entries(self, table, topic=None):
        def add_to_entries(key, value):
            entries[key] = value

        entries = {}
        self._bulk_operation(table, topic, 'GET', entry_cb=add_to_entries)
        LOG.debug('found %d entries', len(entries))
        return list(entries.values())

    def _get_all_keys_from_node(self, node, pattern):
        keys = set()
        cursor = 0
        while True:
            (cursor, partial_keys) = node.client.scan(cursor, match=pattern)
            keys.update(partial_keys)
            if cursor == 0:
                break
        return keys

    def _query_info(self, table, topic, key=None):
        if topic is None:
            # ask all nodes
            pattern = self._key_name(table, '*', key or '*')
            nodes = self._cluster.nodes
        else:
            # ask a specific node
            pattern = self._key_name(table, topic, key or '*')
            nodes = (self._cluster.get_node(pattern), )
        return pattern, nodes

    def _scan(self, table, key=None, topic=None):
        (pattern, nodes) = self._query_info(table, topic, key)
        keys = set()

        for node in nodes:
            retry = 0
            while retry < self.RETRY_COUNT:
                LOG.debug('Getting all keys with pattern %s retry %d',
                          pattern, retry)
                try:
                    node_keys = self._get_all_keys_from_node(node, pattern)
                    keys.update(node_keys)
                    break
                except exceptions.RedisError:
                    LOG.exception('Error getting keys from node %s:%s',
                                  node.ip, node.port)
                    retry += 1
                    self._cluster.populate_cluster()
            if retry == self.RETRY_COUNT:
                raise df_exceptions.DBKeyNotFound('ALL KEYS')
        return keys

    def _key_name_infer_topic(self, table, key):
        raw_keys = self._scan(table, key=key)
        if len(raw_keys) != 1:
            LOG.error('Found %d entries with key "%s" in table %s',
                      len(raw_keys), key, table)
            raise df_exceptions.DBKeyNotFound(key=key)
        return raw_keys.pop()

    def get_all_keys(self, table, topic=None):
        def _strip_table_topic(key):
            match = self._table_strip_re.match(key)
            return match.group(1) if match else key
        raw_keys = self._scan(table, topic=topic)
        keys = [_strip_table_topic(raw_key) for raw_key in raw_keys]
        LOG.debug('found %d keys', len(keys))
        return keys

    def allocate_unique_key(self, table):
        real_key = self._key_name(db_common.UNIQUE_KEY_TABLE, None, table)
        return int(self._key_command('INCR', real_key))

    def process_ha(self):
        pass
