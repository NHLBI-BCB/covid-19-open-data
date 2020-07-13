#!/usr/bin/env python
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import json
import os.path
import datetime
import traceback
from io import BytesIO
from pathlib import Path
from functools import partial
from argparse import ArgumentParser
from tempfile import TemporaryDirectory
from typing import Dict, List

from flask import Flask, request
from google.cloud import storage
from google.oauth2.credentials import Credentials
from google.cloud.storage.blob import Blob

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# pylint: disable=wrong-import-position
from lib.cast import safe_int_cast
from lib.concurrent import thread_map
from lib.constants import SRC, GCS_BUCKET_PROD, GCS_BUCKET_TEST
from lib.io import export_csv
from lib.net import download
from lib.pipeline import DataPipeline
from lib.pipeline_tools import get_table_names
from publish import main as publish_tables

app = Flask(__name__)
BLOB_OP_MAX_RETRIES = 3


def get_storage_client():
    """
    Creates an instance of google.cloud.storage.Client using a token if provided via, otherwise
    the default credentials are used.
    """
    token_env_key = "GCP_TOKEN"
    if os.getenv(token_env_key) is None:
        return storage.Client()
    else:
        credentials = Credentials(os.getenv(token_env_key))
        return storage.Client(credentials=credentials)


def get_storage_bucket(bucket_name: str):
    """
    Gets an instance of the storage bucket for the specified bucket name
    """
    client = get_storage_client()

    # If bucket name is not provided, read it from env var
    bucket_env_key = "GCS_BUCKET_NAME"
    bucket_name = bucket_name or os.getenv(bucket_env_key)
    assert bucket_name is not None, f"{bucket_env_key} not set"
    return client.bucket(bucket_name)


def download_folder(bucket_name: str, remote_path: str, local_folder: Path) -> None:
    bucket = get_storage_bucket(bucket_name)

    def _download_blob(local_folder: Path, blob: Blob) -> None:
        # Remove the prefix from the remote path
        rel_path = blob.name.split(f"{remote_path}/", 2)[-1]
        print(f"Downloading {rel_path} to {local_folder}/")
        file_path = local_folder / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(BLOB_OP_MAX_RETRIES):
            try:
                return blob.download_to_filename(file_path)
            except Exception as exc:
                print(exc, file=sys.stderr)

    map_func = partial(_download_blob, local_folder)
    _ = thread_map(map_func, bucket.list_blobs(prefix=remote_path), total=None, disable=True)
    list(_)  # consume the results


def upload_folder(bucket_name: str, remote_path: str, local_folder: Path) -> None:
    bucket = get_storage_bucket(bucket_name)

    def _upload_file(remote_path: str, file_path: Path):
        print(f"Uploading {file_path} to {remote_path}/")
        target_path = file_path.relative_to(local_folder)
        blob = bucket.blob(os.path.join(remote_path, target_path))
        for _ in range(BLOB_OP_MAX_RETRIES):
            try:
                return blob.upload_from_filename(file_path)
            except Exception as exc:
                print(exc, file=sys.stderr)

    map_func = partial(_upload_file, remote_path)
    _ = thread_map(map_func, local_folder.glob("**/*.*"), total=None, disable=True)
    list(_)  # consume the results


def cache_build_map() -> Dict[str, List[str]]:
    sitemap: Dict[str, List[str]] = {}
    bucket = get_storage_bucket(GCS_BUCKET_TEST)
    for blob in bucket.list_blobs(prefix="cache"):
        filename = blob.name.split("/")[-1]
        if filename == "sitemap.json":
            continue
        sitemap_key = filename.split(".")[0]
        sitemap[sitemap_key] = sitemap.get(sitemap_key, [])
        sitemap[sitemap_key].append(blob.name)

    # Sort all the cache items
    for sitemap_key, snapshot_list in sitemap.items():
        sitemap[sitemap_key] = list(sorted(snapshot_list))

    return sitemap


@app.route("/cache_pull")
def cache_pull() -> None:
    with TemporaryDirectory() as workdir:
        workdir = Path(workdir)
        now = datetime.datetime.utcnow()
        output_folder = workdir / now.strftime("%Y-%m-%d-%H")
        output_folder.mkdir(parents=True, exist_ok=True)

        def _pull_source(cache_source: Dict[str, str]):
            url = cache_source.pop("url")
            output = cache_source.pop("output")
            buffer = BytesIO()
            try:
                download(url, buffer)
                with (output_folder / output).open("wb") as fd:
                    fd.write(buffer.getvalue())
            except:
                print(f"Cache pull failed for {url}")
                traceback.print_exc()

        # Pull each of the sources from the cache config
        _ = thread_map(_pull_source, json.load((SRC / "cache" / "config.json").open("r")))
        list(_)  # consume the results

        # Upload all cached data to the bucket
        upload_folder(GCS_BUCKET_TEST, "cache", workdir)

        # Build the sitemap for all cached files
        sitemap = cache_build_map()
        bucket = get_storage_bucket(GCS_BUCKET_TEST)
        blob = bucket.blob("cache/sitemap.json")
        blob.upload_from_string(json.dumps(sitemap))

    return "OK"


@app.route("/update_table")
def update_table(table_name: str = None, idx: int = None) -> None:
    table_name = table_name or request.args.get("table")
    idx = idx or safe_int_cast(request.args.get("idx"))
    assert table_name in list(get_table_names())
    with TemporaryDirectory() as output_folder:
        output_folder = Path(output_folder)
        (output_folder / "snapshot").mkdir(parents=True, exist_ok=True)
        (output_folder / "intermediate").mkdir(parents=True, exist_ok=True)

        # Load the pipeline configuration given its name
        pipeline_name = table_name.replace("-", "_")
        data_pipeline = DataPipeline.load(pipeline_name)

        # Limit the sources to only the index provided
        if idx is not None:
            data_pipeline.data_sources = [data_pipeline.data_sources[idx]]

        # Produce the intermediate files from the data source
        intermediate_results = data_pipeline.parse(output_folder, process_count=1)
        data_pipeline._save_intermediate_results(
            output_folder / "intermediate", intermediate_results
        )

        # Upload results to the test bucket because these are not prod files
        upload_folder(GCS_BUCKET_TEST, "snapshot", output_folder / "snapshot")
        upload_folder(GCS_BUCKET_TEST, "intermediate", output_folder / "intermediate")

    return "OK"


@app.route("/combine_table")
def combine_table(table_name: str = None) -> None:
    table_name = table_name or request.args.get("table")
    assert table_name in list(get_table_names())
    with TemporaryDirectory() as output_folder:
        output_folder = Path(output_folder)
        (output_folder / "tables").mkdir(parents=True, exist_ok=True)

        # Download all the intermediate files
        download_folder(GCS_BUCKET_TEST, "intermediate", output_folder / "intermediate")

        # Load the pipeline configuration given its name
        pipeline_name = table_name.replace("-", "_")
        data_pipeline = DataPipeline.load(pipeline_name)

        # Re-load all intermediate results
        intermediate_results = data_pipeline._load_intermediate_results(
            output_folder / "intermediate", data_pipeline.data_sources
        )

        # Combine all intermediate results into a single dataframe
        pipeline_output = data_pipeline.combine(intermediate_results)

        # Output combined data to disk
        export_csv(pipeline_output, output_folder / "tables" / f"{table_name}.csv")

        # Upload results to the test bucket because these are not prod files
        upload_folder(GCS_BUCKET_TEST, "tables", output_folder / "tables")

    return "OK"


@app.route("/publish")
def publish() -> None:
    with TemporaryDirectory() as workdir:
        workdir = Path(workdir)
        (workdir / "tables").mkdir(parents=True, exist_ok=True)
        (workdir / "public").mkdir(parents=True, exist_ok=True)

        # Download all the combined tables into our local storage
        download_folder(GCS_BUCKET_TEST, "tables", workdir / "tables")

        # Prepare all files for publishing and add them to the public folder
        publish_tables(workdir / "public", workdir / "tables", show_progress=False)

        # Upload the results to the prod bucket
        upload_folder(GCS_BUCKET_PROD, "", workdir / "public")

    return "OK"


if __name__ == "__main__":

    # Process command-line arguments
    argparser = ArgumentParser()
    argparser.add_argument("--command", type=str, default=None)
    argparser.add_argument("--args", type=str, default=None)
    args = argparser.parse_args()

    # Used only for debugging purposes
    def _start_server():
        app.run(host="127.0.0.1", port=8080, debug=True)

    def _unknown_command(*func_args):
        print(f"Unknown command {args.command}", file=sys.stderr)

    # If a command + args are supplied, call the corresponding function
    {
        "server": _start_server,
        "update_table": update_table,
        "combine_table": combine_table,
        "publish": publish,
        "cache_pull": cache_pull,
    }.get(args.command, _unknown_command)(**json.loads(args.args or "{}"))