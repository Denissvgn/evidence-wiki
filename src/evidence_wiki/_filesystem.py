"""Use the same native filesystem owner as standalone workspace scripts."""

import os as _os

if _os.name == "nt":
    from ._script_host import load_packaged_script, shared_assets_root

    os = load_packaged_script(shared_assets_root(), "_native_fs").os
else:
    os = _os
