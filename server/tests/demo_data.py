import json
from pathlib import Path
import random

from server.data import HOME, LAYERS, Observation
from server.device import Device

SAMPLES = {
    'alerts': [('Rain notice', 'RAIN', 'Rain in 18m'), ('Public warning exercise', 'TEST', 'Exercise only')],
    'transit': [('M29 bus', 'M29', '4m +2'), ('M41 bus', 'M41', '7m +1'), ('Tram sample', 'TRM', '3m')],
    'rail': [('U8 platform', 'U8', '3m +2'), ('U1 platform', 'U1', '6m'), ('Station lift', 'LFT', 'Lift unavailable')],
    'flights': [('DEMO241 A320', 'A320', '3200m NE'), ('DEMO082 turboprop', 'ATR', '1800m S')],
    'traffic': [
        ('Road restriction', 'LANE', '1 lane closed'),
        ('Congestion', 'SLOW', '+6m'),
        ('Cycle detour', 'BIKE', 'Detour 200m'),
    ],
    'bikes': [('Bike station', 'BIKE', '8 bikes / 12 docks'), ('Cargo bike hub', 'CARGO', '2 available')],
    'parking': [('Parking garage', 'P', '42 spaces'), ('Cycle parking', 'PBIKE', '18 spaces')],
    'charging': [('Charging station', 'EV', '2/4 free 22kW'), ('Fast charger', 'DC', '1/2 free 150kW')],
    'cameras': [('Public camera placeholder', 'CAM', 'No video connected')],
    'weather': [('Weather cell', 'WX', '17C W12 UV2'), ('Rain cell', 'RAIN', '0.8mm/h in 18m')],
    'air': [
        ('Air sensor', 'PM', 'PM2.5 8ug/m3'),
        ('Pollen sensor', 'POL', 'Pollen moderate'),
        ('Noise sensor', 'dB', '54dB aggregate'),
    ],
    'water': [('Canal gauge', 'LVL', 'Level stable'), ('River gauge', 'H2O', '+18cm / 2h')],
    'utilities': [('Power notice', 'PWR', 'Maintenance 14:00'), ('Water works', 'WTR', 'Supply notice')],
    'events': [('Neighborhood market', 'MKT', 'Today 10-16'), ('Outdoor concert', 'GIG', 'Starts 19:30')],
    'news': [('Public consultation', 'NEWS', 'Planning notice'), ('Street festival notice', 'NOTE', 'Weekend closure')],
    'places': [('Demo grocery', 'SHOP', 'Hours unknown'), ('Demo hardware shop', 'DIY', 'Hours unknown')],
    'food': [('Demo cafe', 'CAFE', 'Hours unknown'), ('Demo bakery', 'BAKE', 'Hours unknown')],
    'health': [('Demo pharmacy', 'RX', 'Hours unknown'), ('Demo clinic', 'MED', 'Access unknown')],
    'amenities': [
        ('Drinking fountain', 'TAP', 'Status unknown'),
        ('Public toilet', 'WC', 'Access unknown'),
        ('Recycling point', 'BIN', 'Glass / paper'),
    ],
    'access': [('Accessible entrance', 'ACC', 'Step-free example'), ('Stairs', 'STEP', '12 steps')],
    'connectivity': [
        ('Public Wi-Fi listing', 'WIFI', 'No device scanning'),
        ('Network outage notice', 'NET', 'Service notice'),
    ],
    'nature': [('Street tree', 'TREE', 'Linden example'), ('Bird observation', 'BIRD', 'Citizen report')],
    'astronomy': [('ISS predicted pass region', 'ISS', 'Visible 21:06 example')],
    'vessels': [('Ferry example', 'FERRY', 'Departs +12m'), ('Demo boat', 'BOAT', '6km/h east')],
    'works': [('Building permit', 'WORK', 'Published consultation'), ('Road resurfacing', 'ROAD', 'Works notice')],
    'hazards': [('Lightning example', 'BOLT', 'Strike 8m ago'), ('Flood exercise', 'FLOOD', 'Exercise only')],
    'radio': [('Amateur repeater', 'HAM', 'Frequency not verified')],
    'satellite': [('Optical image footprint', 'IMG', 'Acquisition 2d ago')],
    'heritage': [
        ('Historic building', 'HIST', 'Archive record example'),
        ('Public artwork', 'ART', 'Sculpture example'),
    ],
    'sensors': [('Temperature sensor', 'TEMP', '17.2C'), ('Pedestrian counter', 'COUNT', 'Aggregate 142/h')],
}


def make_demo() -> tuple[Observation, ...]:
    seed = json.loads(Path(__file__).with_name('seed_demo.json').read_text())
    observations = []
    for record in seed:
        layer = record['layer']
        if layer == 'environment':
            layer = 'air' if record['id'] == 'air' else 'water'
        observations.append(
            Observation(
                id=record['id'],
                layer=layer,
                title=record['title'],
                short=record['title'].split()[0][:6],
                position=tuple(record['position']),
                value=record['subtitle'],
                sources=tuple(f'{source["provider"]}:{source["recordId"]}' for source in record['sources']),
                age_minutes=record['ageMinutes'],
                warning=record.get('severity') == 'warning',
            )
        )
    rng = random.Random(2432)
    for layer in LAYERS:
        for index in range(9):
            title, short, value = SAMPLES[layer.id][index % len(SAMPLES[layer.id])]
            spread = 0.0025 if index < 6 else 0.011
            position = (HOME[0] + rng.uniform(-spread * 1.6, spread * 1.6), HOME[1] + rng.uniform(-spread, spread))
            sources = (f'demo:{layer.id}:{index}',)
            if layer.id in ('places', 'food') and index % 3 == 0:
                sources = (f'osm:demo-node/{layer.id}-{index}', f'atp:demo-{layer.id}-{index}')
            observations.append(
                Observation(
                    id=f'{layer.id}-{index}',
                    layer=layer.id,
                    title=f'{title} {index + 1:02}',
                    short=short,
                    position=position,
                    value=value,
                    sources=sources,
                    age_minutes=rng.randrange(0, 30),
                    warning=layer.id in ('alerts', 'hazards', 'utilities'),
                    area_radius_m=(85 + index * 12)
                    if layer.id in ('weather', 'hazards', 'utilities', 'satellite')
                    else None,
                )
            )
    return tuple(observations)


OBSERVATIONS = make_demo()
BY_ID = {item.id: item for item in OBSERVATIONS}


class DemoDevice(Device):
    def __init__(self, **kwargs):
        super().__init__(
            **{'mode': 'demo', 'center': HOME, 'home': HOME, 'zoom': 15, 'observations': OBSERVATIONS, **kwargs}
        )


def install_demo_state(app):
    app.state.mode = 'demo'
    app.state.demo_factory = DemoDevice
    app.state.demo_observations = OBSERVATIONS
