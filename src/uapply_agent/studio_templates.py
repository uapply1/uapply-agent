"""Studio template packages for uApply Community: zip a package folder for the server's checks and upload."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

MAX_ZIP_BYTES = 25 * 1024 * 1024      # the server's package limit
SKIP_NAMES = {"__MACOSX", "Thumbs.db"}


class PackageError(ValueError):
    pass


def zip_package(folder: Path) -> bytes:
    """The folder's files as a zip, paths relative to the folder. Hidden files and folders are left out."""
    if not folder.is_dir():
        raise PackageError(f"{folder} is not a folder")
    if not (folder / "template.json").is_file():
        raise PackageError(f"{folder} has no template.json")
    root = folder.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if any(part.startswith(".") or part in SKIP_NAMES for part in rel.parts):
                continue
            # Never follow a link out of the package: it could send a file from elsewhere on the machine.
            if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                continue
            zf.write(path, rel.as_posix())
    data = buf.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise PackageError(f"the package is {len(data) // (1024 * 1024)} MB; the limit is 25 MB")
    return data
