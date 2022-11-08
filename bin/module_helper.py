#!/usr/bin/env python3
# vim: ts=4 et sw=4 sts=4 :

import os
import sys
import pkgutil


def tryFindModule(module):
    parent_dir = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
    sys.path.insert(0, parent_dir)

    if pkgutil.find_loader(module) is None:
        print("The module {} could not be found in '{}'!".format(
            module, parent_dir))

        sys.exit(4)


tryFindModule("rocketterm")
