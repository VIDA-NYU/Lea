"""Exercise external source edits and fixture cleanup as the CI runner user."""
import argparse
import os
from pathlib import Path
import sys

from production_container import docker, production_fixture, replace_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    image = parser.parse_args().image
    for fail in (False, True):
        home = None
        try:
            with production_fixture(image) as (home, containers):
                writer = docker('run', '-d', '-v', f'{home}:/app/prover/workspace',
                                '--entrypoint', 'sleep', image, '300')
                containers.append(writer)
                source = home / 'proofs/Lea/Readiness/Helper.lean'
                mounted = '/app/prover/workspace/proofs/Lea/Readiness'
                docker('exec', writer, '/app/adapter/.venv/bin/python', '-c',
                       'from pathlib import Path; '
                       f'p=Path({mounted!r})/"Helper.lean"; '
                       'p.parent.mkdir(parents=True); p.write_text("original")')
                if sys.platform == 'linux' and os.getuid() != 0:
                    try:
                        source.write_text('host edit')
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError('Root-owned fixture did not reproduce the CI permission failure')
                replace_source(writer, home, source, 'external edit')
                assert source.read_text() == 'external edit'
                # Git must still accept the repository after the external edit.
                docker('exec', writer, 'git', '-C', mounted, 'init')
                docker('exec', writer, 'git', '-C', mounted, 'config', 'user.name', 'Fixture')
                if fail:
                    raise RuntimeError('intentional scenario failure')
        except RuntimeError as exc:
            assert fail and str(exc) == 'intentional scenario failure'
        assert home is not None and not home.exists(), home
        assert docker('ps', '-aq', '--filter', 'id=' + writer) == ''
    print('External source edits, Git ownership, and success/failure cleanup passed')


if __name__ == '__main__':
    main()
