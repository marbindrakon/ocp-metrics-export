#!/usr/bin/env python

from datetime import datetime
from dateutil import parser as date_parser

import csv
import os
import requests
import yaml

def _read_config():
    conf_file = os.environ.get('METRICS_EXPORT_CONFIG', 'config.yaml')
    with open(conf_file) as config_file:
        return yaml.safe_load(config_file)

def _query_snapshot(endpoint, query, timeout="60s", time=None, token=None):
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if token:
        headers['Authorization'] = "Bearer {0}".format(token)
    data = {"query": query, "timeout": timeout}
    if time:
        data['time'] = time
    url = endpoint + "/api/v1/query"
    resp = requests.post(url, headers=headers, data=data)
    resp.raise_for_status()
    resp_json = resp.json()
    if resp_json.get('status', 'failure') != 'success':
        print("Prometheus returned bad status {0}".format(resp_json))
        raise ValueError
    return resp_json['data']

def _query_range(endpoint, query, start, end, step, timeout="60s", token=None):
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if token:
        headers['Authorization'] = "Bearer {0}".format(token)
    url = endpoint + "/api/v1/query"
    data = {"query": query, "timeout": timeout}
    data['start'] = start.timestamp()
    data['end'] = end.timestamp()
    data['step'] = step
    resp = requests.post(url, headers=headers, data=data)
    resp.raise_for_status()
    return resp.json()

def _create_directory_structure(config):
    output_base = os.environ.get('METRICS_EXPORT_OUTDIR', config.get('output_dir'))
    for cluster in config.get('clusters', []):
        if config.get('capture_time_series', True):
            os.makedirs(os.path.join(output_base, 'time_series', 'cluster'), exist_ok=True)
            os.makedirs(os.path.join(output_base, 'time_series', 'nodes'), exist_ok=True)
        if config.get('capture_snapshot', True):
            os.makedirs(os.path.join(output_base, 'snapshot', 'cluster'), exist_ok=True)
            os.makedirs(os.path.join(output_base, 'snapshot', 'nodes'), exist_ok=True)

def _handle_cluster_snapshot_result(result_data):
    if not result_data['result']:
        return None
    result_type = result_data['resultType']
    if result_type in ["scalar", "string"]:
        return result_data['result'][1]
    if result_type == "vector":
        return result_data['result'][0]['value'][1]

def _handle_node_snapshot_result(cluster, result_data):
    if not result_data['result']:
        return {}
    result_type = result_data['resultType']
    if result_type in ["scalar", "string"]:
        # We shouldn't get any scalars or strings
        return {}
    if result_type == "vector":
        return_dict = {}
        for result in result_data['result']:
            node_name = result['metric']['instance'].split(':')[0]
            if node_name in cluster.get('node_merge', {}):
                node_name = cluster['node_merge'][node_name]
            return_dict[node_name] = result['value'][1]
        return return_dict

def _persist_cluster_snapshots(cluster, config, data_dict):
    output_base = os.environ.get('METRICS_EXPORT_OUTDIR', config.get('output_dir'))
    out_file_path = os.path.join(output_base, 'snapshot', 'cluster', 'cluster_snapshots.csv')
    file_exists = os.path.exists(out_file_path)
    with open(out_file_path, mode='a', newline='') as csv_file:
        field_names = data_dict.keys()
        writer = csv.DictWriter(csv_file, fieldnames=field_names)
        if not file_exists:
            writer.writeheader()
        writer.writerow(data_dict)

def _persist_node_snapshots(cluster, config, data_dict):
    output_base = os.environ.get('METRICS_EXPORT_OUTDIR', config.get('output_dir'))
    out_file_path = os.path.join(output_base, 'snapshot', 'nodes', '{0}_node_snapshots.csv'.format(cluster['name']))
    file_exists = os.path.exists(out_file_path)
    # Flatten the node data structure into a list of node-specific dicts
    dicts_to_write = []
    snapshot_time = data_dict['time']
    for key,value in data_dict.items():
        if key == "time":
            continue
        node_dict = {}
        node_dict['cluster'] = cluster['name']
        node_dict['node'] = key
        node_dict['time'] = snapshot_time
        for node_key,node_value in value.items():
            node_dict[node_key] = node_value
        dicts_to_write.append(node_dict)
    
    with open(out_file_path, mode='a', newline='') as csv_file:
        field_names = dicts_to_write[0].keys()
        writer = csv.DictWriter(csv_file, fieldnames=field_names)
        if not file_exists:
            writer.writeheader()
        for record in dicts_to_write:
            writer.writerow(record)

if __name__ == "__main__":
    config = _read_config()
    _create_directory_structure(config)
    if config.get('capture_snapshot', True):
        # Query for snapshot data and create one CSV per cluster
        for cluster in config.get('clusters', []):
            cluster_snapshots = {}
            node_snapshots = {}
            cluster_snapshots['cluster'] = cluster['name']
            snapshot_time = datetime.utcnow().timestamp()
            cluster_snapshots['time'] = snapshot_time
            node_snapshots['time'] = snapshot_time
            for query in config.get('cluster_queries', []):
                try:
                    timeout = query.get('timeout', config['query_timeout'])
                    promql = query['promql'].replace("{{cluster}}", cluster['name'])
                    if query['source'] == "multicluster":
                        endpoint = config['multicluster']['base_url']
                        token = config['multicluster']['token']
                    else:
                        endpoint = cluster['prom_base_url']
                        token = cluster['token']
                    result_data = _query_snapshot(endpoint, promql, timeout=timeout, token=token)
                    cluster_snapshots[query['name']] = _handle_cluster_snapshot_result(result_data)
                except ValueError:
                    # Record null value for bad query
                    cluster_snapshots[query['name']] = None
            _persist_cluster_snapshots(cluster, config, cluster_snapshots)
            for query in config.get('node_queries', []):
                try:
                    timeout = query.get('timeout', config['query_timeout'])
                    promql = query['promql'].replace("{{cluster}}", cluster['name'])
                    if query['source'] == "multicluster":
                        endpoint = config['multicluster']['base_url']
                        token = config['multicluster']['token']
                    else:
                        endpoint = cluster['prom_base_url']
                        token = cluster['token']
                    result_data = _query_snapshot(endpoint, promql, timeout=timeout, token=token)
                    node_results = _handle_node_snapshot_result(cluster, result_data)
                    for instance, value in node_results.items():
                        if instance not in node_snapshots:
                            node_snapshots[instance] = {}
                        node_snapshots[instance][query['name']] = value
                except ValueError:
                    pass
            _persist_node_snapshots(cluster, config, node_snapshots)

