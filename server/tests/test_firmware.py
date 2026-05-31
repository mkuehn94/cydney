from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('g++'), reason='Native firmware tests need g++')
def test_touch_gestures(tmp_path):
    firmware = Path(__file__).resolve().parents[2] / 'firmware'
    executable = tmp_path / 'gestures'
    subprocess.run(
        [
            'g++',
            '-std=c++11',
            '-Wall',
            '-Wextra',
            '-Werror',
            '-I',
            str(firmware / 'include'),
            str(firmware / 'tests/gesture.cpp'),
            '-o',
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
