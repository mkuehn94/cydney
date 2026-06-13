# What is shown at which zoom

320 x 240 is not a lot, so not everything can be visible all the time. The rules are in [`server/display.py`](../server/display.py). "close" means zoom 16-18.

| Symbol | What | Visible | How solid |
| --- | --- | --- | --- |
| `!` | warnings | always, never clustered | fully |
| `A`, `T` | aircraft / traffic | always | almost fully |
| `C`, `W`, `Q`, `~`, `X`, `*` | cameras, weather, air, water, utilities, events | always | almost fully |
| `N` | public notices | always | fully |
| `B`, `U` | bus / subway | always | faint when zoomed out, solid when close |
| `SC`, `BK` | scooters / bikes | clustered until z16, single from z17 | same fade |
| `P`, `E` | parking / charging | clustered | same fade |
| `S`, `F` | shops / food | close only | mostly solid |
| `H`, `K` | health / roadworks | always | rather faint |
| `WC`, `H2O`, `FIX`, `ATM` | toilets, water, bike repair, ATM | always | about half |
| `=`, `h` | entrances / heritage | close only | mostly solid |

Some more things:

- warnings always win, they are drawn last and at full opacity
- old data gets an amber corner on the marker
- areas (warning zones etc) are see-through so you still see the map below
- station pins only show up close, otherwise Munich is just one big blob of stops
- hidden markers can't be tapped
