"""Create an isolated AgOpenWeb profile for the FleetPhoenix GPS feed.

    python3 agopenweb_setup.py agopenweb-data

Then start AgOpenWeb with that profile (macOS shown; Windows: set AGOPENWEB_DATA and run the app):

    AGOPENWEB_DATA="$PWD/agopenweb-data" /Applications/AgOpenWeb.app/Contents/MacOS/AgOpenWeb.Desktop --headless

The profile switches AgOpenWeb's own simulator off (so it listens for GPS on UDP 9999) and
adds the "FleetPhoenix Farm" field, whose boundary contains the three FleetPhoenix fields.
It never touches your normal AgOpenWeb settings.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from agopenweb_bridge import BOUNDARY, LAT0, LON0

FIELD_NAME = 'FleetPhoenix Farm'


def write_profile(data_dir):
    root = Path(data_dir).resolve() / 'AgOpenWeb'
    root.mkdir(parents=True, exist_ok=True)
    settings_path = root / 'appsettings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings.update(simulatorEnabled=False, imageryVisible=False, ntripEnabled=False)
    settings_path.write_text(json.dumps(settings, indent=2) + '\n')
    state_path = root / 'appstate.json'
    if not state_path.exists():
        state_path.write_text(json.dumps({'webCameraPitch': 0, 'webCameraZoom': 0.7, 'webCameraMode': 1,
                                          'simulatorLatitude': LAT0, 'simulatorLongitude': LON0}, indent=2) + '\n')
    field = root / 'Fields' / FIELD_NAME
    field.mkdir(parents=True, exist_ok=True)
    (field / 'Field.txt').write_text(
        f"{datetime.now():%Y-%B-%d %I:%M:%S %p}\n$FieldDir\n{FIELD_NAME}\n$Offsets\n0,0\nConvergence\n0\n"
        f"StartFix\n{LAT0:.8f},{LON0:.8f}\n")
    w, e, s, n = BOUNDARY
    (field / 'Boundary.txt').write_text(
        f"$Boundary\nFalse\n4\n{w},{n},0\n{e},{n},1.57080\n{e},{s},3.14159\n{w},{s},4.71239\n")
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('data', type=Path, help='folder for the dedicated AgOpenWeb profile')
    root = write_profile(parser.parse_args().data)
    print(f'AgOpenWeb profile ready: {root.parent}')
    print(f'Field "{FIELD_NAME}" added; AgOpenWeb simulator off, listening for GPS on UDP 9999.')
