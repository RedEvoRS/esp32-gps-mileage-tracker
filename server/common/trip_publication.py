"""Order ingest publication and viewer year snapshots on the shared data mount."""

import os
from contextlib import contextmanager
from threading import Lock


# Windows runs the services together in local tests. Deployed containers use
# the kernel lock below so separate processes and read-only mounts participate.
_local_lock = Lock()


@contextmanager
def trip_publication_lock(data_root):
    if os.name != "posix":
        with _local_lock:
            yield
        return

    import fcntl

    # Both containers see the same directory inode. A directory opened for
    # reading can hold a flock on Linux, with no lock file to create or replace.
    fd = os.open(data_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
