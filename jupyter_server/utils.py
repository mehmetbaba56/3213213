"""Notebook related utilities"""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import asyncio
import errno
import importlib.util
import inspect
import os
import socket
import sys
from _frozen_importlib_external import _NamespacePath
from contextlib import contextmanager
from urllib.parse import quote
from urllib.parse import SplitResult
from urllib.parse import unquote
from urllib.parse import urljoin  # noqa: F401
from urllib.parse import urlparse
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import pathname2url  # noqa: F401

from packaging.version import Version
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPClient
from tornado.httpclient import HTTPRequest
from tornado.netutil import Resolver
from tornado.web import HTTPError


def url_path_join(*pieces):
    """Join components of url into a relative url

    Use to prevent double slash when joining subpath. This will leave the
    initial and final / in place
    """
    initial = pieces[0].startswith("/")
    final = pieces[-1].endswith("/")
    stripped = [s.strip("/") for s in pieces]
    result = "/".join(s for s in stripped if s)
    if initial:
        result = "/" + result
    if final:
        result = result + "/"
    if result == "//":
        result = "/"
    return result


def url_is_absolute(url):
    """Determine whether a given URL is absolute"""
    return urlparse(url).path.startswith("/")


def path2url(path):
    """Convert a local file path to a URL"""
    pieces = [quote(p) for p in path.split(os.sep)]
    # preserve trailing /
    if pieces[-1] == "":
        pieces[-1] = "/"
    url = url_path_join(*pieces)
    return url


def url2path(url):
    """Convert a URL to a local file path"""
    pieces = [unquote(p) for p in url.split("/")]
    path = os.path.join(*pieces)
    return path


def url_escape(path):
    """Escape special characters in a URL path

    Turns '/foo bar/' into '/foo%20bar/'
    """
    parts = path.split("/")
    return "/".join([quote(p) for p in parts])


def url_unescape(path):
    """Unescape special characters in a URL path

    Turns '/foo%20bar/' into '/foo bar/'
    """
    return "/".join([unquote(p) for p in path.split("/")])


def samefile_simple(path, other_path):
    """
    Fill in for os.path.samefile when it is unavailable (Windows+py2).

    Do a case-insensitive string comparison in this case
    plus comparing the full stat result (including times)
    because Windows + py2 doesn't support the stat fields
    needed for identifying if it's the same file (st_ino, st_dev).

    Only to be used if os.path.samefile is not available.

    Parameters
    ----------
    path : String representing a path to a file
    other_path : String representing a path to another file

    Returns
    -------
    same:   Boolean that is True if both path and other path are the same
    """
    path_stat = os.stat(path)
    other_path_stat = os.stat(other_path)
    return path.lower() == other_path.lower() and path_stat == other_path_stat


def to_os_path(path, root=""):
    """Convert an API path to a filesystem path

    If given, root will be prepended to the path.
    root must be a filesystem path already.
    """
    parts = path.strip("/").split("/")
    parts = [p for p in parts if p != ""]  # remove duplicate splits
    path = os.path.join(root, *parts)
    return path


def to_api_path(os_path, root=""):
    """Convert a filesystem path to an API path

    If given, root will be removed from the path.
    root must be a filesystem path already.
    """
    if os_path.startswith(root):
        os_path = os_path[len(root) :]
    parts = os_path.strip(os.path.sep).split(os.path.sep)
    parts = [p for p in parts if p != ""]  # remove duplicate splits
    path = "/".join(parts)
    return path


def check_version(v, check):
    """check version string v >= check

    If dev/prerelease tags result in TypeError for string-number comparison,
    it is assumed that the dependency is satisfied.
    Users on dev branches are responsible for keeping their own packages up to date.
    """
    try:
        return Version(v) >= Version(check)
    except TypeError:
        return True


# Copy of IPython.utils.process.check_pid:


def _check_pid_win32(pid):
    import ctypes

    # OpenProcess returns 0 if no such process (of ours) exists
    # positive int otherwise
    return bool(ctypes.windll.kernel32.OpenProcess(1, 0, pid))


def _check_pid_posix(pid):
    """Copy of IPython.utils.process.check_pid"""
    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.ESRCH:
            return False
        elif err.errno == errno.EPERM:
            # Don't have permission to signal the process - probably means it exists
            return True
        raise
    else:
        return True


if sys.platform == "win32":
    check_pid = _check_pid_win32
else:
    check_pid = _check_pid_posix


async def ensure_async(obj):
    """Convert a non-awaitable object to a coroutine if needed,
    and await it if it was not already awaited.
    """
    if inspect.isawaitable(obj):
        try:
            result = await obj
        except RuntimeError as e:
            if str(e) == "cannot reuse already awaited coroutine":
                # obj is already the coroutine's result
                return obj
            raise
        return result
    # obj doesn't need to be awaited
    return obj


def run_sync(maybe_async):
    """If async, runs maybe_async and blocks until it has executed,
    possibly creating an event loop.
    If not async, just returns maybe_async as it is the result of something
    that has already executed.

    Parameters
    ----------
    maybe_async : async or non-async object
        The object to be executed, if it is async.

    Returns
    -------
    result
        Whatever the async object returns, or the object itself.
    """
    if not inspect.isawaitable(maybe_async):
        # that was not something async, just return it
        return maybe_async
    # it is async, we need to run it in an event loop
    def wrapped():
        create_new_event_loop = False
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            create_new_event_loop = True
        else:
            if loop.is_closed():
                create_new_event_loop = True
        if create_new_event_loop:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(maybe_async)
        except RuntimeError as e:
            if str(e) == "This event loop is already running":
                # just return a Future, hoping that it will be awaited
                result = asyncio.ensure_future(maybe_async)
            else:
                raise e
        return result

    return wrapped()


async def run_sync_in_loop(maybe_async):
    """Runs a function synchronously whether it is an async function or not.

    If async, runs maybe_async and blocks until it has executed.

    If not async, just returns maybe_async as it is the result of something
    that has already executed.

    Parameters
    ----------
    maybe_async : async or non-async object
        The object to be executed, if it is async.

    Returns
    -------
    result
        Whatever the async object returns, or the object itself.
    """
    if not inspect.isawaitable(maybe_async):
        return maybe_async
    return await maybe_async


def urlencode_unix_socket_path(socket_path):
    """Encodes a UNIX socket path string from a socket path for the `http+unix` URI form."""
    return socket_path.replace("/", "%2F")


def urldecode_unix_socket_path(socket_path):
    """Decodes a UNIX sock path string from an encoded sock path for the `http+unix` URI form."""
    return socket_path.replace("%2F", "/")


def urlencode_unix_socket(socket_path):
    """Encodes a UNIX socket URL from a socket path for the `http+unix` URI form."""
    return "http+unix://%s" % urlencode_unix_socket_path(socket_path)


def unix_socket_in_use(socket_path):
    """Checks whether a UNIX socket path on disk is in use by attempting to connect to it."""
    if not os.path.exists(socket_path):
        return False

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
    except socket.error:
        return False
    else:
        return True
    finally:
        sock.close()


@contextmanager
def _request_for_tornado_client(urlstring, method="GET", body=None, headers=None):
    """A utility that provides a context that handles
    HTTP, HTTPS, and HTTP+UNIX request.
    Creates a tornado HTTPRequest object with a URL
    that tornado's HTTPClients can accept.
    If the request is made to a unix socket, temporarily
    configure the AsyncHTTPClient to resolve the URL
    and connect to the proper socket.
    """
    parts = urlsplit(urlstring)
    if parts.scheme in ["http", "https"]:
        pass
    elif parts.scheme == "http+unix":
        # If unix socket, mimic HTTP.
        parts = SplitResult(
            scheme="http",
            netloc=parts.netloc,
            path=parts.path,
            query=parts.query,
            fragment=parts.fragment,
        )

        class UnixSocketResolver(Resolver):
            """A resolver that routes HTTP requests to unix sockets
            in tornado HTTP clients.
            Due to constraints in Tornados' API, the scheme of the
            must be `http` (not `http+unix`). Applications should replace
            the scheme in URLS before making a request to the HTTP client.
            """

            def initialize(self, resolver):
                self.resolver = resolver

            def close(self):
                self.resolver.close()

            async def resolve(self, host, port, *args, **kwargs):
                return [(socket.AF_UNIX, urldecode_unix_socket_path(host))]

        resolver = UnixSocketResolver(resolver=Resolver())
        AsyncHTTPClient.configure(None, resolver=resolver)
    else:
        raise Exception("Unknown URL scheme.")

    # Yield the request for the given client.
    url = urlunsplit(parts)
    request = HTTPRequest(url, method=method, body=body, headers=headers)
    yield request


def fetch(urlstring, method="GET", body=None, headers=None):
    """
    Send a HTTP, HTTPS, or HTTP+UNIX request
    to a Tornado Web Server. Returns a tornado HTTPResponse.
    """
    with _request_for_tornado_client(urlstring) as request:
        response = HTTPClient(AsyncHTTPClient).fetch(request)
    return response


async def async_fetch(urlstring, method="GET", body=None, headers=None, io_loop=None):
    """
    Send an asynchronous HTTP, HTTPS, or HTTP+UNIX request
    to a Tornado Web Server. Returns a tornado HTTPResponse.
    """
    with _request_for_tornado_client(urlstring) as request:
        response = await AsyncHTTPClient(io_loop).fetch(request)
    return response


def is_namespace_package(namespace):
    """Is the provided namespace a Python Namespace Package (PEP420).

    https://www.python.org/dev/peps/pep-0420/#specification

    Returns `None` if module is not importable.

    """
    # NOTE: using submodule_search_locations because the loader can be None
    try:
        spec = importlib.util.find_spec(namespace)
    except ValueError:  # spec is not set - see https://docs.python.org/3/library/importlib.html#importlib.util.find_spec
        return None

    if not spec:
        # e.g. module not installed
        return None
    return isinstance(spec.submodule_search_locations, _NamespacePath)


def authorized(action, resource=None, message=None):
    """A decorator for tornado.web.RequestHandler methods
    that verifies whether the current user is authorized
    to make the following request.

    Helpful for adding an 'authorization' layer to
    a REST API.

    Parameters
    ----------
    action : str
        the type of permission or action to check.

    resource: str or None
        the name of the resource the action is being authorized
        to access.

    message : str or none
        a message for the unauthorized action.
    """
    # Get message
    if message is None:
        "User is not authorized to make this request."

    error = HTTPError(status_code=401, log_message=message)

    def wrapper(method):
        def inner(self, *args, **kwargs):
            user = self.current_user
            # If the user is allowed to do this action,
            # call the method.
            if self.user_is_authorized(user, action, resource):
                return method(self, *args, **kwargs)
            # else raise an exception.
            else:
                raise error

        return inner

    return wrapper
