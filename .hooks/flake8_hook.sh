#!/bin/sh

tmpdir=$(mktemp -d /tmp/rt.git.commit.XXXXXX)
trap "rm -rf $tmpdir" EXIT

git checkout-index --prefix="$tmpdir/" -af
pushd "$tmpdir" >/dev/null
python ./tools/run_flake8
RES=$?
popd >/dev/null

if [ $RES -ne 0 ]; then
	echo "flake8 check failed, aborting commit"
	exit 1
fi
