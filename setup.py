#!/usr/bin/env python3
# vim: ts=4 et sw=4 sts=4 :

from __future__ import with_statement, print_function

from setuptools import setup
import os, sys
import glob

pkg_root = os.path.abspath(os.path.dirname(__file__))
readme_rst = os.path.join( pkg_root, "README.rst" )
remove_rst = False

def getLongDesc():
    global remove_rst

    if not os.path.exists(readme_rst):
        # dynamically generate a restructured text formatted long description
        # from markdown for setuptools to use
        import subprocess
        pandoc = "/usr/bin/pandoc"
        if not os.path.exists(pandoc):
            print("Can't generate RST readme from MD readme, because pandoc isn't installed. Skipping long description.", file = sys.stderr)
            return "no long description available"
        subprocess.check_call(
            [ pandoc, "-f", "markdown", "-t", "rst", "-o", "README.rst", "README.md" ],
            shell = False,
            close_fds = True
        )
        remove_rst = True

    with open(readme_rst, 'r') as rst_file:
        long_desc = rst_file.read()

    return long_desc

long_desc = getLongDesc()

try:

    setup(
        name = 'rocket.term',
        version = '0.2.0-r2',
        description = 'rocket.term is a text based chat client for the Rocket.chat messaging solution',
        long_description = long_desc,
        author = 'Matthias Gerstner',
        author_email = 'matthias.gerstner@nefkom.net',
        license = 'GPL2',
        keywords = 'Rocket.chat messaging chat terminal',
        packages = ['rocketterm'],
        install_requires = ['urwid',"requests","websocket_client"],
        url = 'https://github.com/gerstner-hub/rocket.term',
        package_data = {
            # it is practically impossible to automatically install a
            # configuration file or template configuration file in the actual
            # file system, there are subtle differences between the package
            # formats used ... setuptools create a compressed egg and
            # sometimes an uncompressed one, depending on whether the code
            # accesses __file__. See also here:
            #     https://github.com/pypa/setuptools/issues/460
            # this file here will be included in the compressed egg file in
            # our case. Accessing it requires explicit knowledge about
            # setuptools in the code using the pkg_resource module.
            #
            # these paths are all relative to the package name and we can't
            # access files outside of this directory.
            'rocketterm': ['etc/*.ini']
        },
        classifiers = [
            'Intended Audience :: Developers',
            'Intended Audience :: End Users/Desktop',
            'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
            'Programming Language :: Python :: 3.7',
            'Topic :: Communications :: Chat',
            'Topic :: Terminals'
        ],
        scripts = [ 'bin/rocketterm' ]
    )
finally:
    try:
        if remove_rst:
            os.remove(readme_rst)
    except:
        pass
