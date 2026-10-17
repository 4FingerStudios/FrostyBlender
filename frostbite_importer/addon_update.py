"""Install a verified release beside the loaded package; activate it on Blender restart.

No unregister/reload: the current scene and add-on preferences remain in memory.
This module intentionally has no bpy dependency so archive and rollback checks run outside Blender.
"""
import hashlib
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Invalid add-on version')
    return tuple(map(int, value.split('.')))


def install(archive, digest, expected_version, current_version, blender_version, cache, target):
    """Validate entirely before changing files; restore the old directory on commit failure."""
    archive, cache, target = Path(archive).resolve(), Path(cache).resolve(), Path(target).resolve()
    if archive.parent != cache or archive.suffix != '.zip' or not archive.is_file():
        raise ValueError('Update ZIP must be in the FrostyBlender updates folder')
    if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest or ''):
        raise ValueError('Missing update checksum')
    if archive.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('Update ZIP is too large')
    with archive.open('rb') as stream:
        actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual.lower() != digest.lower():
        raise ValueError('Update checksum mismatch')
    if version(expected_version) <= version(current_version):
        raise ValueError('The installed add-on is already this version or newer')
    if not (target / 'blender_manifest.toml').is_file():
        raise ValueError('Installed add-on manifest is missing')
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 10000 or sum(m.file_size for m in members) > 256 * 1024 * 1024:
            raise ValueError('Update archive is too large')
        names = set()
        for member in members:
            path = PurePosixPath(member.filename)
            name = member.filename.rstrip('/').lower()
            if (not path.parts or path.is_absolute() or '..' in path.parts or
                    '\\' in member.filename or ':' in member.filename or
                    any(part.endswith(('.', ' ')) for part in path.parts) or
                    stat.S_ISLNK(member.external_attr >> 16) or name in names):
                raise ValueError('Unsafe or duplicate path in update archive')
            names.add(name)
        if not {'blender_manifest.toml', '__init__.py', 'app_link.py', 'addon_update.py'} <= names:
            raise ValueError('ZIP is not a Frostbite Map Importer update with updater support')
        manifest = tomllib.loads(source.read('blender_manifest.toml').decode('utf-8'))
        if manifest.get('id') != 'frostbite_importer' or manifest.get('version') != expected_version:
            raise ValueError('Update manifest does not match the release')
        if tuple(blender_version[:3]) < version(manifest.get('blender_version_min', '4.2.0')):
            raise ValueError('This update requires a newer Blender version')
        maximum = manifest.get('blender_version_max')
        if maximum and tuple(blender_version[:3]) >= version(maximum):
            raise ValueError('This update does not support this Blender version')
        # Both renames stay on the same filesystem. Keep the original directory for recovery.
        stage = Path(tempfile.mkdtemp(prefix='.frostbite-update-', dir=target.parent))
        backup = stage.with_name(stage.name + '-backup')
        moved = False
        try:
            source.extractall(stage)
            target.rename(backup)
            moved = True
            try:
                stage.rename(target)
            except BaseException:
                backup.rename(target)
                moved = False
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        if moved:
            # A locked backup can be removed later; installation has already succeeded.
            shutil.rmtree(backup, ignore_errors=True)
    return expected_version
