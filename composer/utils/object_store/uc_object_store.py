# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""Databricks Unity Catalog Volumes object store."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import uuid
from typing import Callable, List, Optional

from composer.utils.import_helpers import MissingConditionalImportError
from composer.utils.object_store.object_store import ObjectStore, ObjectStoreTransientError

log = logging.getLogger(__name__)

__all__ = ['UCObjectStore']

_NOT_FOUND_ERROR_CODE = 'NOT_FOUND'


def _wrap_errors(uri: str, e: Exception):
    from databricks.sdk.core import DatabricksError
    if isinstance(e, DatabricksError):
        if e.error_code == _NOT_FOUND_ERROR_CODE:  # type: ignore
            raise FileNotFoundError(f'Object {uri} not found') from e
    raise ObjectStoreTransientError from e


class UCObjectStore(ObjectStore):
    """Utility class for uploading and downloading data from Databricks Unity Catalog (UC) Volumes.

    .. note::

        Using this object store requires setting `DATABRICKS_HOST` and `DATABRICKS_TOKEN`
        environment variables with the right credentials to be able to access the files in
        the unity catalog volumes.

    Args:
        path (str): The Databricks UC Volume path that is of the format
            `Volumes/<catalog-name>/<schema-name>/<volume-name>/path/to/folder`.
            Note that this prefix should always start with /Volumes and adhere to the above format
            since this object store only suports Unity Catalog Volumes and
            not other Databricks Filesystems.
    """

    _UC_VOLUME_LIST_API_ENDPOINT = '/api/2.0/fs/list'

    def __init__(self, path: str) -> None:
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError as e:
            raise MissingConditionalImportError('databricks', conda_package='databricks-sdk>=0.8.0,<1.0') from e

        try:
            self.client = WorkspaceClient()
        except Exception as e:
            raise ValueError(
                f'Databricks SDK credentials not correctly setup. '
                'Visit https://databricks-sdk-py.readthedocs.io/en/latest/authentication.html#databricks-native-authentication '
                'to identify different ways to setup credentials.') from e
        self.prefix = self.validate_path(path)
        self.client = WorkspaceClient()

    @staticmethod
    def validate_path(path: str) -> str:
        """Parses the given path to extract the UC Volume prefix from the path.

        .. note::

            This function only uses the first 4 directories from the path to construct the
            UC Volumes prefix and will ignore the rest of the directories in the path

        Args:
            path (str): The Databricks UC Volume path of the format
            `Volumes/<catalog-name>/<schema-name>/<volume-name>/path/to/folder`.
        """
        path = os.path.normpath(path)
        if not path.startswith('Volumes'):
            raise ValueError('Databricks Unity Catalog Volumes paths should start with "Volumes".')

        dirs = path.split(os.sep)
        if len(dirs) < 4:
            raise ValueError(f'Databricks Unity Catalog Volumes path expected to be of the format '
                             '`Volumes/<catalog-name>/<schema-name>/<volume-name>/<optional-path>`. '
                             f'Found path={path}')

        # The first 4 dirs form the prefix
        return os.path.join(*dirs[:4])

    def _get_object_path(self, object_name: str) -> str:
        """Return the absolute Single Path Namespace for the given object_name.

        Args:
            object_name (str): Absolute or relative path of the object w.r.t. the
            UC Volumes root.
        """
        # convert object name to relative path if prefix is included
        if os.path.commonprefix([object_name, self.prefix]) == self.prefix:
            object_name = os.path.relpath(object_name, start=self.prefix)
        return os.path.join('/', self.prefix, object_name)

    def get_uri(self, object_name: str) -> str:
        """Returns the URI for ``object_name``.

        .. note::

            This function does not check that ``object_name`` is in the object store.
            It computes the URI statically.

        Args:
            object_name (str): The object name.

        Returns:
            str: The URI for ``object_name`` in the object store.
        """
        return f'dbfs:{self._get_object_path(object_name)}'

    def upload_object(self,
                      object_name: str,
                      filename: str | pathlib.Path,
                      callback: Callable[[int, int], None] | None = None) -> None:
        """Upload a file from local to UC volumes.

        Args:
            object_name (str): Name of the stored object in UC volumes w.r.t. volume root.
            filename (str | pathlib.Path): Path the the object on disk
            callback ((int, int) -> None, optional): Unused
        """
        # remove unused variable
        del callback
        with open(filename, 'rb') as f:
            self.client.files.upload(self._get_object_path(object_name), f)

    def download_object(self,
                        object_name: str,
                        filename: str | pathlib.Path,
                        overwrite: bool = False,
                        callback: Callable[[int, int], None] | None = None) -> None:
        """Download the given object from UC Volumes to the specified filename.

        Args:
            object_name (str): The name of the object to download i.e. path relative to the root of the volume.
            filename (str | pathlib.Path): The local path where a the file needs to be downloaded.
            overwrite(bool, optional): Whether to overwrite an existing file at ``filename``, if it exists.
                (default: ``False``)
            callback ((int) -> None, optional): Unused

        Raises:
            FileNotFoundError: If the file was not found in UC volumes.
            ObjectStoreTransientError: If there was any other error querying the Databricks UC volumes that should be retried.
        """
        # remove unused variable
        del callback

        if os.path.exists(filename) and not overwrite:
            raise FileExistsError(f'The file at {filename} already exists and overwrite is set to False.')

        dirname = os.path.dirname(filename)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        tmp_path = str(filename) + f'{uuid.uuid4()}.tmp'

        try:
            from databricks.sdk.core import DatabricksError
            try:
                with self.client.files.download(self._get_object_path(object_name)).contents as resp:
                    with open(tmp_path, 'wb') as f:
                        # Chunk the data into multiple blocks of 64MB to avoid
                        # OOMs when downloading really large files
                        for chunk in iter(lambda: resp.read(64 * 1024 * 1024), b''):
                            f.write(chunk)
            except DatabricksError as e:
                _wrap_errors(self.get_uri(object_name), e)
        except:
            # Make best effort attempt to clean up the temporary file
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        else:
            if overwrite:
                os.replace(tmp_path, filename)
            else:
                os.rename(tmp_path, filename)

    def get_object_size(self, object_name: str) -> int:
        """Get the size of the object in UC volumes in bytes.

        Args:
            object_name (str): The name of the object.

        Returns:
            int: The object size, in bytes.

        Raises:
            FileNotFoundError: If the file was not found in the object store.
        """
        from databricks.sdk.core import DatabricksError
        try:
            file_info = self.client.files.get_status(self._get_object_path(object_name))
            return file_info.file_size
        except DatabricksError as e:
            _wrap_errors(self.get_uri(object_name), e)

    def list_objects(self, prefix: Optional[str]) -> List[str]:
        """List all objects in the object store with the given prefix.

         .. note::

            This function removes the directories from the returned list.

        Args:
            prefix (str): The prefix to search for.

        Returns:
            list[str]: A list of object names that match the prefix.
        """
        if not prefix:
            prefix = self.prefix

        from databricks.sdk.core import DatabricksError
        try:
            data = json.dumps({'path': self._get_object_path(prefix)})
            # NOTE: This API is in preview and should not be directly used outside of this instance
            resp = self.client.api_client.do(method='GET',
                                             path=self._UC_VOLUME_LIST_API_ENDPOINT,
                                             data=data,
                                             headers={'Source': 'mosaicml/composer'})
            return [f['path'] for f in resp.get('files', []) if not f['is_dir']]
        except DatabricksError as e:
            _wrap_errors(self.get_uri(prefix), e)
