import datetime as dt
import urllib.parse as urlparse

from collections import defaultdict
from concurrent import futures

import pandas as pd
import param
import requests

from .base import Source, cached
from ..util import parse_timedelta


class PrometheusSource(Source):
    """
    Queries a Prometheus PromQL endpoint for timeseries information
    about Kubernetes pods.
    """

    ids = param.List(default=[], doc="""
       List of pod IDs to query.""")

    metrics = param.List(
        default=['memory_usage', 'cpu_usage', 'network_receive_bytes'],
        doc="Names of metric queries to execute")

    promql_api = param.String(doc="""
       Name of the AE5 deployment exposing the Prometheus API""")

    period = param.String(default='3h', doc="""
        Period to query over specified as a string. Supports:

          - Week:   '1w'
          - Day:    '1d'
          - Hour:   '1h'
          - Minute: '1m'
          - Second: '1s'
    """)

    step = param.String(default='10s', doc="""
        Step value to use in PromQL query_range query.""")

    source_type = 'prometheus'

    _memory_usage_query = """sum by(container_name)
    (container_memory_usage_bytes{job="kubelet",
    cluster="", namespace="default", pod_name=POD_NAME,
    container_name=~"app|app-proxy", container_name!="POD"})"""

    _network_receive_bytes_query = """sort_desc(sum by (pod_name)
    (rate(container_network_receive_bytes_total{job="kubelet", cluster="",
    namespace="default", pod_name=POD_NAME}[1m])))"""

    _cpu_usage_query = """sum by (container_name)
    (rate(container_cpu_usage_seconds_total{job="kubelet", cluster="",
     namespace="default", image!="", pod_name=POD_NAME,
     container_name=~"app|app-proxy", container_name!="POD"}[1m]))"""

    _metrics = {
        'memory_usage': {
            'query': _memory_usage_query,
            'schema': {"type": "number"}
        },
        'network_receive_bytes': {
            'query': _network_receive_bytes_query,
            'schema': {"type": "number"}
        },
        'cpu_usage': {
            'query': _cpu_usage_query,
            'schema': {"type": "number"}
        }
    }

    def _format_timestamps(self):
        end = dt.datetime.now()
        end_formatted = end.isoformat("T") + "Z"
        period = parse_timedelta(self.period)
        if period is None:
            raise ValueError(f"Could not parse period '{self.period}'. "
                             "Must specify weeks ('1w'), days ('1d'), "
                             "hours ('1h'), minutes ('1m'), or "
                             "seconds ('1s').")
        start = end - period
        start_formatted = start.isoformat("T") + "Z"
        return start_formatted, end_formatted

    def _url_query_parameters(self, pod_id, query):
        """
        Uses regular expression to map ae5-tools pod_id to full id.
        """
        start_timestamp, end_timestamp = self._format_timestamps()
        regexp = f'anaconda-app-{pod_id}-.*'
        query = query.replace("pod_name=POD_NAME", f"pod_name=~'{regexp}'")
        query = query.replace("pod=POD_NAME", f"pod=~'{regexp}'")
        query = query.replace('\n',' ')
        query = query.replace(' ','%20')
        query_escaped = query.replace('\"','%22')
        return f'query={query_escaped}&start={start_timestamp}&end={end_timestamp}&step={self.step}'

    def _get_query_url(self, metric, pod_id):
        "Return the full query URL"
        query_template = self._metrics[metric]['query']
        query_params = self._url_query_parameters(pod_id, query_template)
        return f'{self.promql_api}/query_range?{query_params}'

    def _get_query_json(self, query_url):
        "Function called in parallel to fetch JSON in ThreadPoolExecutor"
        response = requests.get(query_url, verify=False)
        data = response.json()
        if len(data) == 0:
            return None
        return data

    def _json_to_df(self, metric, response_json):
        "Convert JSON response to pandas DataFrame"
        df = pd.DataFrame(response_json, columns=['timestamp', metric])
        df[metric] = df[metric].astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        return df.set_index('timestamp')

    def _fetch_data(self, pod_ids):
        "Returns fetched JSON in dictionary indexed by pod_id then metric name"
        if not pod_ids:
            return {}
        # ToDo: Remove need to slice
        triples = [
            (pod_id, metric, self._get_query_url(metric, pod_id[3:]))
            for pod_id in pod_ids for metric in self.metrics 
        ]
        fetched_json = defaultdict(dict)
        with futures.ThreadPoolExecutor(len(triples)) as executor:
            tasks = {executor.submit(self._get_query_json, query_url):
                     (pod_id, metric, query_url)
                     for pod_id, metric, query_url in triples}
            for future in futures.as_completed(tasks):
                (pod_id, metric, query_url) = tasks[future]
                try:
                    fetched_json[pod_id][metric] = future.result()
                except Exception as e:
                    fetched_json[pod_id][metric] = []
                    self.param.warning(
                        f"Could not fetch {metric} for pod {pod_id}. "
                        f"Query used: {query_url}, errored with {type(e)}({e})."
                    )
        return fetched_json

    def _make_query(self, **query):
        pod_ids = [
            pod_id for pod_id in self.ids
            if 'id' not in query or pod_id in query['id']
        ]
        json_data = self._fetch_data(pod_ids)
        dfs = []
        for pod_id, pod_data in json_data.items():
            df = None
            for metric in self.metrics:
                data_df = self._json_to_df(metric, pod_data[metric])
                if df is None:
                    df = data_df
                else:
                    df = pd.merge(df, data_df, on='timestamp', how='outer')
            df = df.reset_index()
            df.insert(0, 'id', pod_id)
            dfs.append(df)
        if dfs:
            return pd.concat(dfs)
        else:
            return pd.DataFrame(columns=list(self.get_schema('timeseries')))

    def get_schema(self, table=None):
        schema = {
            "id": {"type": "string", "enum": self.ids},
            "timestamp" : {"type": "string", "format": "datetime"}
        }
        for k, mdef in self._metrics.items():
            schema[k] = mdef['schema']
        return {"timeseries": schema} if table is None else schema

    @cached(with_query=True)
    def get(self, table, **query):
        if table not in ('timeseries',):
            raise ValueError(f"PrometheusSource has no '{table}' table, "
                             "it currently only has a 'timeseries' table.")
        return self._make_query(**query)