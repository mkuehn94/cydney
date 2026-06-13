# CYD + VPS

The ESP32 joins your Wi-Fi, gets a session from the server, downloads 320 x 240 JPEGs and sends touches back. Everything else happens on the server.

Two firmware targets, depends on the display controller of your board (the number of USB ports does NOT tell you which one you have, I tried):

| Target | Display |
| --- | --- |
| `cyd` | ILI9341 (default, this is what mine has) |
| `cyd2usb` | ST7789 |

Only the 2.8 inch resistive touch boards (XPT2046). Pins are in [platformio.ini](../firmware/platformio.ini).

## Server

Docker Compose + Caddy. Caddy does HTTPS and the password, the app itself has no auth. A small VPS with 2 GB RAM is enough. Only run ONE worker, sessions are kept in memory.

```sh
rsync -az --exclude='.git' --exclude='.venv' --exclude='node_modules' \
  --exclude='.cache' --exclude='.pio' --exclude='dist' \
  --include='.env.example' --exclude='.env*' ./ user@vps:~/cydney/
ssh user@vps
cd ~/cydney/deploy
cp .env.example .env
chmod 600 .env
```

Create a password hash and put it into `deploy/.env` together with your domain and username (keep the single quotes around the hash, bcrypt has `$` in it):

```sh
docker run --rm -it caddy:2-alpine caddy hash-password
```

```sh
docker compose up -d --build
docker compose logs --tail=80 app caddy
```

Now `https://your-domain/` should ask for the password.

If you already run nginx on that machine (like me) use [compose.nginx.yaml](../deploy/compose.nginx.yaml) and include [nginx.conf](../deploy/nginx.conf) in your server block. Set `CYD_ROOT_PATH` if it lives under a prefix like `/cydney`.

One thing that cost me an evening: the ESP32 needs about 6 seconds for a P-521 TLS handshake and then times out. The nginx snippet sets `ssl_ecdh_curve X25519:prime256v1`, with that it's fine.

## Flashing

You need [PlatformIO](https://docs.platformio.org/en/latest/core/installation/index.html) (`uv tool install platformio`).

```sh
pio device list
pio run -d firmware -e cyd -t upload --upload-port /dev/ttyUSB0
```

If it hangs at `Connecting...` hold BOOT, tap RESET, let go of BOOT when it starts writing.

## First boot

1. Touch calibration: hold each of the 4 crosses until it moves on.
2. Join the **Cydney-Setup** Wi-Fi with your phone, the password is shown on the display.
3. Open http://192.168.4.1, pick your Wi-Fi (2.4 GHz only) and enter the server URL, user and password.
4. Save, it should connect and show the map.

Hold BOOT for 2 seconds to get back into the setup.

Browser and CYD share the same session, so if you pan in the browser the display follows and vice versa. While dragging on the CYD the image is just moved locally, the real frame comes when you let go. Depending on your Wi-Fi a tap takes 0.5 - 1 s until you see the result.

Serial monitor (115200 baud), `c` recalibrates touch, `s` opens the setup:

```sh
pio device monitor -d firmware --baud 115200
```

## Updating

rsync again, then

```sh
cd ~/cydney/deploy
docker compose up -d --build
```

Don't use `docker compose down -v`, that deletes the caches and the certificates.
