# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import glob
import hashlib
import logging
import os
import shutil
import zipfile
import tools.config
from contextlib import suppress
from tools import helpers

# Properties that the native bridge (ARM translation) layers manage, and that
# are safe for us to add/remove from the [properties] section of waydroid.cfg.
MANAGED_PROPS = [
    "ro.product.cpu.abilist",
    "ro.product.cpu.abilist32",
    "ro.product.cpu.abilist64",
    "ro.dalvik.vm.native.bridge",
    "ro.enable.native.bridge.exec",
    "ro.vendor.enable.native.bridge.exec",
    "ro.vendor.enable.native.bridge.exec64",
    "ro.ndk_translation.version",
    "ro.dalvik.vm.isa.arm",
    "ro.dalvik.vm.isa.arm64",
]

# The translation prebuilt archives are proprietary, pulled from the same
# community-maintained mirrors that waydroid_script uses. Each entry maps the
# Android version to (download url, md5 checksum). "13" matches the Lineage 20
# (Android 13) images, "11" the Lineage 18.1 (Android 11) images.
BACKENDS = {
    "libndk": {
        "dl_links": {
            "13": (
                "https://github.com/supremegamers/vendor_google_proprietary_ndk_translation-prebuilt/archive/68734c52556d3d7a6db34c603dd9276915c29f2f.zip",
                "0b2207c490fcb400aa5c87fcf0d52d38"),
            "11": (
                "https://github.com/supremegamers/vendor_google_proprietary_ndk_translation-prebuilt/archive/9324a8914b649b885dad6f2bfd14a67e5d1520bf.zip",
                "c9572672d1045594448068079b34c350"),
        },
        "props": {
            "ro.product.cpu.abilist": "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi",
            "ro.product.cpu.abilist32": "x86,armeabi-v7a,armeabi",
            "ro.product.cpu.abilist64": "x86_64,arm64-v8a",
            "ro.dalvik.vm.native.bridge": "libndk_translation.so",
            "ro.enable.native.bridge.exec": "1",
            "ro.vendor.enable.native.bridge.exec": "1",
            "ro.vendor.enable.native.bridge.exec64": "1",
            "ro.ndk_translation.version": "0.2.3",
            "ro.dalvik.vm.isa.arm": "x86",
            "ro.dalvik.vm.isa.arm64": "x86_64",
        },
        "files": [
            "bin/arm",
            "bin/arm64",
            "bin/ndk_translation_program_runner_binfmt_misc",
            "bin/ndk_translation_program_runner_binfmt_misc_arm64",
            "etc/binfmt_misc",
            "etc/ld.config.arm.txt",
            "etc/ld.config.arm64.txt",
            "etc/init/ndk_translation.rc",
            "lib/arm",
            "lib64/arm64",
            "lib/libndk*",
            "lib64/libndk*",
        ],
    },
    "libhoudini": {
        "dl_links": {
            "13": (
                "https://github.com/supremegamers/vendor_intel_proprietary_houdini/archive/9e77896350caccd228b36b2e1b4a994aa4bd48da.zip",
                "3807fe029559db3037efe245d9e74270"),
            "11": (
                "https://github.com/supremegamers/vendor_intel_proprietary_houdini/archive/81f2a51ef539a35aead396ab7fce2adf89f46e88.zip",
                "fbff756612b4144797fbc99eadcb6653"),
        },
        "props": {
            "ro.product.cpu.abilist": "x86_64,x86,arm64-v8a,armeabi-v7a,armeabi",
            "ro.product.cpu.abilist32": "x86,armeabi-v7a,armeabi",
            "ro.product.cpu.abilist64": "x86_64,arm64-v8a",
            "ro.dalvik.vm.native.bridge": "libhoudini.so",
            "ro.enable.native.bridge.exec": "1",
            "ro.dalvik.vm.isa.arm": "x86",
            "ro.dalvik.vm.isa.arm64": "x86_64",
        },
        "files": [
            "bin/arm",
            "bin/arm64",
            "bin/houdini",
            "bin/houdini64",
            "etc/binfmt_misc",
            "etc/init/houdini.rc",
            "lib/arm",
            "lib/libhoudini.so",
            "lib64/arm64",
            "lib64/libhoudini.so",
        ],
    },
}

# Registers the binfmt_misc handlers that libhoudini needs to execute ARM
# binaries. Written to /system/etc/init/houdini.rc inside the container.
HOUDINI_INIT_RC = """on early-init
    mount binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

on property:ro.enable.native.bridge.exec=1
    exec -- /system/bin/sh -c "echo ':arm_exe:M::\\x7f\\x45\\x4c\\x46\\x01\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x02\\x00\\x28::/system/bin/houdini:P' > /proc/sys/fs/binfmt_misc/register"
    exec -- /system/bin/sh -c "echo ':arm_dyn:M::\\x7f\\x45\\x4c\\x46\\x01\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x03\\x00\\x28::/system/bin/houdini:P' >> /proc/sys/fs/binfmt_misc/register"
    exec -- /system/bin/sh -c "echo ':arm64_exe:M::\\x7f\\x45\\x4c\\x46\\x02\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x02\\x00\\xb7::/system/bin/houdini64:P' >> /proc/sys/fs/binfmt_misc/register"
    exec -- /system/bin/sh -c "echo ':arm64_dyn:M::\\x7f\\x45\\x4c\\x46\\x02\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x03\\x00\\xb7::/system/bin/houdini64:P' >> /proc/sys/fs/binfmt_misc/register"
"""

def _md5sum(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _within(base, target):
    base = os.path.normpath(base)
    target = os.path.normpath(target)
    return target == base or target.startswith(base + os.sep)

def _container_stopped(args):
    if os.path.exists(tools.config.defaults["lxc"] + "/waydroid"):
        return helpers.lxc.status(args) == "STOPPED"
    return True

def _overlay_system(args):
    return tools.config.defaults["overlay"] + "/system"

def _manifest_path(args):
    return args.work + "/arm_translation_manifest"

def _prebuilts_prefix(zf):
    prefix = ""
    for name in zf.namelist():
        idx = name.find("/prebuilts/")
        if idx >= 0:
            candidate = name[:idx + len("/prebuilts/")]
        elif name.startswith("prebuilts/"):
            candidate = "prebuilts/"
        else:
            continue
        if not prefix or len(candidate) < len(prefix):
            prefix = candidate
    return prefix

def _extract(zf, prefix, dest):
    extracted = []
    for name in zf.namelist():
        if not name.startswith(prefix):
            continue
        rel = os.path.relpath(name, prefix)
        if not rel or rel.startswith("..") or os.path.isabs(rel):
            continue
        target = os.path.join(dest, rel)
        if not _within(dest, target):
            continue
        info = zf.getinfo(name)
        if name.endswith("/"):
            os.makedirs(target, exist_ok=True)
            extracted.append(rel)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = (info.external_attr >> 16) & 0xFFFF
        os.chmod(target, mode if mode else 0o644)
        extracted.append(rel)
    return extracted

def _apply_props(cfg, backend):
    for key in MANAGED_PROPS:
        cfg["properties"].pop(key, None)
    for key, value in BACKENDS[backend]["props"].items():
        cfg["properties"][key] = value
    cfg["waydroid"]["arm_translation"] = backend

def _remove_paths(base, rels):
    target_set = set()
    for rel in rels:
        target = os.path.normpath(os.path.join(base, rel))
        if _within(base, target):
            target_set.add(target)
    for target in sorted(target_set, reverse=True):
        with suppress(OSError):
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)

def _remove_glob(base, patterns):
    for pattern in patterns:
        for target in glob.glob(os.path.join(base, pattern)):
            if not _within(base, target):
                continue
            with suppress(OSError):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)

def _prune_empty_dirs(base):
    base = os.path.normpath(base)
    if not os.path.isdir(base):
        return
    for root, dirs, files in os.walk(base, topdown=False):
        for name in dirs:
            path = os.path.join(root, name)
            with suppress(OSError):
                if not os.listdir(path):
                    os.rmdir(path)

def _clean(args, cfg):
    overlay_system = _overlay_system(args)
    manifest = _manifest_path(args)
    backend = cfg["waydroid"].get("arm_translation", "None")
    if os.path.isfile(manifest):
        with open(manifest) as f:
            rels = [line.strip() for line in f if line.strip()]
        _remove_paths(overlay_system, rels)
        with suppress(OSError):
            os.remove(manifest)
    elif backend in BACKENDS:
        _remove_glob(overlay_system, BACKENDS[backend]["files"])
    _prune_empty_dirs(overlay_system)

def install(args, backend, android_version="13", source=None):
    if backend not in BACKENDS:
        raise ValueError("Unknown ARM translation backend: {}".format(backend))

    cfg = tools.config.load(args)
    arch = cfg["waydroid"].get("arch", "None")
    if arch not in ("x86", "x86_64"):
        raise ValueError("ARM translation is only supported on x86/x86_64 hosts"
                         " (detected arch: {}). ARM hosts run ARM apps natively.".format(arch))

    if not _container_stopped(args):
        raise RuntimeError("WayDroid container is running. Stop it first with"
                           " 'waydroid session stop' before installing ARM translation.")

    if cfg["waydroid"].get("mount_overlays") != "True":
        logging.warning("Mounting overlays is disabled, the translation libraries"
                        " will not be visible inside the container.")

    if android_version not in BACKENDS[backend]["dl_links"]:
        raise ValueError("No {} prebuilt for Android version {}".format(backend, android_version))

    url, md5sum = BACKENDS[backend]["dl_links"][android_version]

    if source:
        zip_path = os.path.abspath(source)
        if not os.path.isfile(zip_path):
            raise OSError("Translation archive not found: " + zip_path)
    else:
        zip_path = helpers.http.download(args, url, "arm_translation", cache=True)
        if _md5sum(zip_path) != md5sum:
            logging.error("Downloaded archive hash doesn't match, removing it.")
            with suppress(OSError):
                os.remove(zip_path)
            raise ValueError("Failed to verify {} archive checksum".format(backend))

    overlay_system = _overlay_system(args)
    os.makedirs(overlay_system, exist_ok=True)

    _clean(args, cfg)

    with zipfile.ZipFile(zip_path, "r") as zf:
        prefix = _prebuilts_prefix(zf)
        if not prefix:
            logging.warning("No 'prebuilts' directory found in the archive, extracting from the root.")
        extracted = _extract(zf, prefix, overlay_system)
        if not extracted:
            raise ValueError("Failed to extract {} archive".format(backend))

    if backend == "libhoudini":
        init_rc = os.path.join(overlay_system, "etc", "init", "houdini.rc")
        os.makedirs(os.path.dirname(init_rc), exist_ok=True)
        with open(init_rc, "w") as f:
            f.write(HOUDINI_INIT_RC)
        os.chmod(init_rc, 0o644)
        extracted.append("etc/init/houdini.rc")

    with open(_manifest_path(args), "w") as f:
        f.write("\n".join(sorted(set(extracted))) + "\n")

    _apply_props(cfg, backend)
    tools.config.save(args, cfg)

    logging.info("Installed {} ARM translation libraries into {}".format(backend, overlay_system))
    logging.info("Restart Waydroid for the changes to take effect:"
                 " 'waydroid session stop', then 'waydroid session start'.")

def remove(args):
    cfg = tools.config.load(args)
    backend = cfg["waydroid"].get("arm_translation", "None")
    _clean(args, cfg)

    for key in MANAGED_PROPS:
        if key in cfg["properties"]:
            del cfg["properties"][key]
    cfg["waydroid"]["arm_translation"] = "None"
    tools.config.save(args, cfg)

    if backend in BACKENDS:
        logging.info("Removed {} ARM translation libraries.".format(backend))
    else:
        logging.info("No ARM translation backend was configured, cleaned up managed properties.")
    logging.info("Restart Waydroid for the changes to take effect.")