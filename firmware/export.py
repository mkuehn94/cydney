import hashlib
from pathlib import Path
import subprocess

Import('env')  # comes from platformio


def export_firmware(source, target, env):
    project = Path(env.subst('$PROJECT_DIR'))
    output = project / 'dist'
    output.mkdir(exist_ok=True)
    name = f'cydney-{env.subst("$PIOENV")}.bin'
    image = output / name
    uploader = Path(env.PioPlatform().get_package_dir('tool-esptoolpy')) / 'esptool.py'
    command = [
        env.subst('$PYTHONEXE'),
        str(uploader),
        '--chip',
        'esp32',
        'merge_bin',
        '--flash_mode',
        'dio',
        '--flash_freq',
        '40m',
        '--flash_size',
        '4MB',
        '-o',
        str(image),
    ]
    for offset, path in env['FLASH_EXTRA_IMAGES']:
        command.extend([offset, env.subst(path)])
    command.extend([env.subst('$ESP32_APP_OFFSET'), str(target[0])])
    subprocess.run(command, check=True)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (output / f'{name}.sha256').write_text(f'{digest}  {name}\n')
    print(f'USB image: {image} (flash at 0x0)')


env.AddPostAction('$BUILD_DIR/${PROGNAME}.bin', export_firmware)
