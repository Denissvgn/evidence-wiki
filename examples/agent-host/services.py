"""Explicit owner-private context services; never selected by worker content."""

import os
import stat
from pathlib import Path

from evidence_wiki import BoundedSubprocessExecutor


def load_executor(path, root):
    supplied=Path(path).expanduser().absolute()
    if supplied.is_symlink() or supplied.resolve()!=supplied:
        raise ValueError('Select a canonical owner-private service factory.')
    descriptor=os.open(supplied,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077
                or info.st_nlink!=1 or info.st_size>65536):
            raise ValueError('Select an owner-private regular service factory, at most 64 KiB.')
        data=b''
        while chunk:=os.read(descriptor,min(65536,65537-len(data))):
            data+=chunk
            if len(data)>65536:
                raise ValueError('Service factory exceeds byte bound.')
        fields=('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        frozen=tuple(getattr(info,name) for name in fields)
        if any(tuple(getattr(row,name) for name in fields)!=frozen for row in (os.fstat(descriptor),supplied.stat())):
            raise ValueError('Service factory changed during selection.')
    finally:
        os.close(descriptor)
    namespace={'__name__':'selected_context_services','__file__':str(supplied)}
    try:
        exec(compile(data,str(supplied),'exec'),namespace)  # noqa: S102 - explicit trusted host selection.
        executor=namespace['build_context_executor'](root)
    except (Exception,SystemExit):
        raise ValueError('Selected context services could not be loaded.') from None
    if type(executor) is not BoundedSubprocessExecutor or any(
            row.model_ref.startswith(('simulated/','offline/','fixture/')) for row in executor.profile.roles):
        raise ValueError('Select a native bounded executor with live model identities.')
    return executor
