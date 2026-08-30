# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import tools.config
from tools import helpers

def install(args):
    helpers.native_bridge.install(
        args, args.backend,
        android_version=getattr(args, "android_version", "13"),
        source=getattr(args, "source", None))

def remove(args):
    helpers.native_bridge.remove(args)

def status(args):
    cfg = tools.config.load(args)
    backend = cfg["waydroid"].get("arm_translation", "None")
    if backend == "None":
        logging.info("No ARM translation backend is installed.")
    else:
        logging.info("ARM translation backend installed: {}".format(backend))