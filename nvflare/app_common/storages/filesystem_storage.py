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

import ast
import json
import os
import shutil
from pathlib import Path
from typing import ByteString, List, Tuple

from nvflare.apis.storage import StorageSpec
from nvflare.apis.utils.format_check import validate_class_methods_args

URI_ROOT = os.path.abspath(os.sep)


@validate_class_methods_args
class FilesystemStorage(StorageSpec):
    def __init__(self, root_dir=URI_ROOT):
        """Init FileSystemStorage.

        Uses local filesystem to persist objects, with absolute paths as object URIs.

        Args:
            root_dir: the absolute path serving as the root of the storage.
            All URIs are rooted at this root_dir.
        """

        if not os.path.isabs(root_dir):
            raise ValueError("root_dir {} must be an absolute path".format(root_dir))
        self.root_dir = root_dir

    def _write(self, path: str, content):
        try:
            Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
            with open(path + "_tmp", "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            if os.path.isfile(path + "_tmp"):
                os.remove(path + "_tmp")
            raise IOError("failed to write content: {}".format(e))

        os.rename(path + "_tmp", path)

    def _read(self, path: str) -> bytes:
        try:
            with open(path, "rb") as f:
                content = f.read()
        except Exception as e:
            raise IOError("failed to read content: {}".format(e))

        return content

    def _object_exists(self, uri: str):
        data_exists = os.path.isfile(os.path.join(uri, "data"))
        meta_exists = os.path.isfile(os.path.join(uri, "meta"))
        return all((os.path.isabs(uri), os.path.isdir(uri), data_exists, meta_exists))

    def create_object(self, uri: str, data: ByteString, meta: dict, overwrite_existing: bool = False):
        """Create a new object or update an existing object

        Args:
            uri: URI of the object
            data: content of the object
            meta: meta info of the object
            overwrite_existing: whether to overwrite the object if already exists

        Returns:

        Raises:
            TypeError: if invalid argument types
            RuntimeError:
                - if error creating the object
                - if object already exists and overwrite_existing is False
                - if object will be inside prexisiting object
                - if object will be at a non-empty directory
            IOError: if error writing the object

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if self._object_exists(full_uri) and not overwrite_existing:
            raise RuntimeError("object {} already exists and overwrite_existing is False".format(uri))

        path_parts = Path(uri).parts
        for i in range(1, len(path_parts)):
            parent_path = str(Path(*path_parts[0:i]))
            if self._object_exists(os.path.join(self.root_dir, parent_path.lstrip(URI_ROOT))):
                raise RuntimeError("cannot create object {} inside preexisting object {}".format(uri, parent_path))

        if not self._object_exists(full_uri) and (os.path.exists(full_uri) and os.listdir(full_uri)):
            raise RuntimeError("cannot create object {} at nonempty directory".format(uri))

        data_path = os.path.join(full_uri, "data")
        meta_path = os.path.join(full_uri, "meta")

        self._write(data_path + "_tmp", data)
        try:
            self._write(meta_path, json.dumps(str(meta)).encode("utf-8"))
        except Exception as e:
            os.remove(data_path + "_tmp")
            raise e
        os.rename(data_path + "_tmp", data_path)

    def update_meta(self, uri: str, meta: dict, replace: bool):
        """Update the meta info of the specified object

        Args:
            uri: URI of the object
            meta: value of new meta info
            replace: whether to replace the current meta completely or partial update

        Returns:

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist
            IOError: if error writing the object

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        if replace:
            self._write(os.path.join(full_uri, "meta"), json.dumps(str(meta)).encode("utf-8"))
        else:
            prev_meta = self.get_meta(uri)
            prev_meta.update(meta)
            self._write(os.path.join(full_uri, "meta"), json.dumps(str(prev_meta)).encode("utf-8"))

    def update_data(self, uri: str, data: ByteString):
        """Update the data info of the specified object

        Args:
            uri: URI of the object
            data: value of new data

        Returns:

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist
            IOError: if error writing the object

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        self._write(os.path.join(full_uri, "data"), data)

    def list_objects(self, dir_path: str) -> List[str]:
        """List all objects in the specified path.

        Args:
            path: the path to the objects

        Returns:
            list of URIs of objects

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if path does not exist

        """
        full_dir_path = os.path.join(self.root_dir, dir_path.lstrip(URI_ROOT))

        if os.path.isdir(full_dir_path):
            return [
                os.path.join(dir_path, obj)
                for obj in os.listdir(full_dir_path)
                if self._object_exists(os.path.join(full_dir_path, obj))
            ]
        else:
            raise RuntimeError("path {} does not exist".format(dir_path))

    def get_meta(self, uri: str) -> dict:
        """Get user defined meta info of the specified object

        Args:
            uri: URI of the object

        Returns:
            meta info of the object.

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        return ast.literal_eval(json.loads(self._read(os.path.join(full_uri, "meta")).decode("utf-8")))

    def get_full_meta(self, uri: str) -> dict:
        """Get full meta info of the specified object

        Args:
            uri: URI of the object

        Returns:
            meta info of the object.

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist

        """
        return self.get_meta(uri)

    def get_data(self, uri: str) -> bytes:
        """Get data of the specified object

        Args:
            uri: URI of the object

        Returns:
            data of the object.

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        return self._read(os.path.join(full_uri, "data"))

    def get_detail(self, uri: str) -> Tuple[dict, bytes]:
        """Get both data and meta of the specified object

        Args:
            uri: URI of the object

        Returns:
            meta info and data of the object.

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        return self.get_meta(uri), self.get_data(uri)

    def delete_object(self, uri: str):
        """Delete specified object

        Args:
            uri: URI of the object

        Returns:

        Raises:
            TypeError: if invalid argument types
            RuntimeError: if object does not exist

        """
        full_uri = os.path.join(self.root_dir, uri.lstrip(URI_ROOT))

        if not self._object_exists(full_uri):
            raise RuntimeError("object {} does not exist".format(uri))

        shutil.rmtree(full_uri)

    def finalize(self):
        """Finalize storage

        Remove empty directories.

        """
        for root, dirnames, _ in os.walk(self.root_dir, topdown=False):
            for dir in dirnames:
                try:
                    os.rmdir(os.path.join(root, dir))
                except OSError:
                    pass