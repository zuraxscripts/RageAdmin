<div align="center">
  <img src="templates/logo.png" alt="RageAdmin logo" width="190" />

  <h1>RageAdmin</h1>

  <p><strong>Modern web control panel for RageMP servers on Linux.</strong></p>

  <p>
    Start, stop, and monitor your server, manage files and resources, control players,
    schedule restarts, track updates, and keep everything in one browser UI.
  </p>

  <p>
    <img alt="Linux x86_64 only" src="https://img.shields.io/badge/Linux-x86__64%20only-111827?style=for-the-badge&logo=linux&logoColor=white">
    <img alt="Python 3.9+" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
    <img alt="MariaDB or MySQL" src="https://img.shields.io/badge/Database-MariaDB%20%7C%20MySQL-0f766e?style=for-the-badge">
    <a href="./egg-rageadmin-ragemp.json"><img alt="Pterodactyl egg included" src="https://img.shields.io/badge/Pterodactyl-Egg%20Included-1f6feb?style=for-the-badge"></a>
  </p>

  <p>
    <a href="#overview">Overview</a>
    <span>&nbsp;|&nbsp;</span>
    <a href="#quick-start">Quick Start</a>
    <span>&nbsp;|&nbsp;</span>
    <a href="#ubuntu-installation">Ubuntu Installation</a>
    <span>&nbsp;|&nbsp;</span>
    <a href="#pterodactyl">Pterodactyl</a>
    <span>&nbsp;|&nbsp;</span>
    <a href="#configuration">Configuration</a>
    <span>&nbsp;|&nbsp;</span>
    <a href="#troubleshooting">Troubleshooting</a>
  </p>
</div>

> [!IMPORTANT]
> RageAdmin is intended for Linux `x86_64` / `amd64` only.
> Windows and ARM / ARM64 are not supported.

> [!TIP]
> If you already run **Pterodactyl Panel**, that is the recommended deployment path.
> This repository already includes [`egg-rageadmin-ragemp.json`](./egg-rageadmin-ragemp.json).

## Overview

RageAdmin is a web-based management panel for RageMP servers.
It gives you a browser UI for live server control, console access, file operations, player management, resource and addon control, scheduled restarts, update checks, multi-user access, and Discord integration.

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>Runtime Control</strong><br />
      Start, stop, restart, watch live status, monitor uptime, and manage scheduled restarts.
    </td>
    <td width="33%" valign="top">
      <strong>Operations</strong><br />
      Use the live console, manage files, edit <code>conf.json</code>, and control resources and addons.
    </td>
    <td width="33%" valign="top">
      <strong>Administration</strong><br />
      Manage players, warnings, kick and ban actions, users, permissions, logs, and Discord integration.
    </td>
  </tr>
</table>

## Feature Highlights

| Area | Included |
| --- | --- |
| Server | Start, stop, restart, status overview, uptime, scheduled restarts |
| Console | Live console stream, command input, clear and scroll controls |
| Files | Upload, download, edit, rename, delete, compress, extract |
| RageMP Config | Panel-side `conf.json` management |
| Runtime Content | Resource and addon management |
| Players | Player list, profiles, warnings, kick, ban, direct messages |
| Access Control | User accounts, roles, permissions, admin action logs |
| Updates | Panel update checker and RageMP server file update checks |
| Integrations | Optional Discord bot and live status embed integration |
| Setup | First-run setup wizard and automatic RageMP Linux package download |

## Support Matrix

| Target | Status |
| --- | --- |
| Linux | Supported |
| Ubuntu 22.04 / 24.04 | Recommended |
| `x86_64` / `amd64` | Supported |
| Windows | Not supported |
| ARM / ARM64 | Not supported |

## Requirements

| Requirement | Notes |
| --- | --- |
| Python | `3.9+` |
| Database | MySQL or MariaDB |
| Tools | `git`, `pip` |
| Network | Internet access during first setup |
| OS | Ubuntu recommended |

Recommended stack:

- Ubuntu 24.04 LTS
- Python 3.11
- MariaDB
- Nginx reverse proxy
- `systemd` service for startup

## Quick Start

If you want the shortest full install flow on Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip mariadb-server
git clone https://github.com/zuraxscripts/RageAdmin.git
cd RageAdmin
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python3 main.py --port 20000
```

Then open:

```text
http://YOUR_SERVER_IP:20000
```

## First Run

On first launch the panel opens a setup flow in the browser and asks for:

1. The setup PIN shown in the console
2. Your admin username and password
3. MariaDB / MySQL connection details

During setup, RageAdmin will:

1. Save and validate the database connection
2. Create the required tables
3. Download the official [RageMP](https://rage.mp/) Linux server archive
4. Extract server files into `RageMP-Server/ragemp-srv`
5. Generate runtime files such as `conf.json` when needed
6. Create the first admin account

Default server executable path:

```text
./RageMP-Server/ragemp-srv/ragemp-server
```

Default panel port:

```text
20000
```

## Ubuntu Installation

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip mariadb-server
```

If you want a reverse proxy:

```bash
sudo apt install -y nginx
```

### 2. Create a database

Log into MariaDB:

```bash
sudo mysql
```

Create a database and user:

```sql
CREATE DATABASE rageadmin CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'rageadmin'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON rageadmin.* TO 'rageadmin'@'127.0.0.1';
FLUSH PRIVILEGES;
EXIT;
```

If the panel will connect from another machine or container, use the correct host instead of `127.0.0.1`.

### 3. Clone the repository

```bash
git clone https://github.com/zuraxscripts/RageAdmin.git
cd RageAdmin
```

### 4. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 5. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 6. Start the panel

```bash
python3 main.py --port 20000
```

Open:

```text
http://YOUR_SERVER_IP:20000
```

### 7. Complete setup

1. Enter the setup PIN from the console
2. Create the admin account
3. Enter the MariaDB / MySQL connection details
4. Wait for the panel to finish downloading and preparing the server

## Custom Port

You can run RageAdmin on a different port:

```bash
python3 main.py --port 8080
```

You can also use environment variables:

```bash
PANEL_PORT=8080 python3 main.py
```

or:

```bash
PORT=8080 python3 main.py
```

## Firewall

If `ufw` is enabled:

```bash
sudo ufw allow 20000/tcp
sudo ufw reload
```

## Pterodactyl

If you already run **Pterodactyl**, this is the recommended deployment method.

Included egg:

- [`egg-rageadmin-ragemp.json`](./egg-rageadmin-ragemp.json)

### What the egg does

- Installs `git`, `curl`, and `python3`
- Clones this repository into the server directory
- Creates a startup wrapper that syncs RageMP `conf.json` to the primary allocation
- Starts the panel with:

```bash
bash /home/container/start-rageadmin.sh
```

### Importing the egg

Import the JSON file into your Pterodactyl panel and create a server from that egg.
The egg exposes:

- Primary allocation `SERVER_PORT` for the RageMP game server
- One additional allocation on `SERVER_PORT + 1` for RageMP package transfer / HTTP
- `PANEL_PORT` for the RageAdmin web panel on a third allocation

After the server starts, open the allocated port in your browser and complete the same setup flow.

### Pterodactyl notes

- Linux only
- `amd64` / `x86_64` only
- Windows nodes are not supported
- ARM nodes are not supported
- The panel still needs a working MySQL / MariaDB database
- First setup still needs outbound internet access to download [RageMP](https://rage.mp/) server files
- In current Pterodactyl, extra allocations still need to be assigned manually in Allocation Management
- `PANEL_PORT` must not match `SERVER_PORT` or `SERVER_PORT + 1`

## Configuration

### Default Paths

| Item | Value |
| --- | --- |
| Panel port | `20000` |
| Server executable | `./RageMP-Server/ragemp-srv/ragemp-server` |
| Server directory | `./RageMP-Server/ragemp-srv/` |
| Logs directory | `./data/logs/` |

### Important Files and Directories

| Path | Purpose |
| --- | --- |
| [`main.py`](./main.py) | Launcher |
| [`server_manager.py`](./server_manager.py) | Main web panel server |
| [`updater.py`](./updater.py) | Built-in updater worker |
| [`requirements.txt`](./requirements.txt) | Python dependencies |
| [`egg-rageadmin-ragemp.json`](./egg-rageadmin-ragemp.json) | Pterodactyl egg |
| `panel_config.json` | Panel configuration |
| [`update_config.json`](./update_config.json) | Update source configuration |
| `data/` | Runtime data, logs, DB config, update state |
| `RageMP-Server/` | Downloaded RageMP server files |
| [`templates/`](./templates) | HTML templates and static assets |
| [`locales/`](./locales) | UI translations |

<details>
<summary><strong>Environment variables</strong></summary>

<br />

Optional runtime variables:

```bash
PANEL_PORT=20000
PORT=20000
PANEL_PRODUCTION=true
PANEL_ACCESS_LOGS=false
PANEL_FORCE_HTTPS=false
PANEL_SESSION_COOKIE_SECURE=false
PANEL_SOCKETIO_ASYNC_MODE=threading
```

Optional database variables:

```bash
HAPPINESS_DB_HOST=127.0.0.1
HAPPINESS_DB_PORT=3306
HAPPINESS_DB_USER=rageadmin
HAPPINESS_DB_PASSWORD=CHANGE_ME
HAPPINESS_DB_NAME=rageadmin
```

If the `HAPPINESS_DB_*` variables are set, the panel can load database settings from the environment instead of the local DB config file.

</details>

<details>
<summary><strong>systemd service example</strong></summary>

<br />

Create a dedicated user:

```bash
sudo useradd -r -m -s /usr/sbin/nologin hpm
```

Clone the project:

```bash
sudo mkdir -p /opt/rageadmin
sudo chown -R hpm:hpm /opt/rageadmin
sudo -u hpm git clone https://github.com/zuraxscripts/RageAdmin.git /opt/rageadmin/app
```

Create the virtual environment and install dependencies:

```bash
sudo -u hpm python3 -m venv /opt/rageadmin/app/.venv
sudo -u hpm /opt/rageadmin/app/.venv/bin/pip install --upgrade pip
sudo -u hpm /opt/rageadmin/app/.venv/bin/pip install -r /opt/rageadmin/app/requirements.txt
```

Create the service file:

```bash
sudo nano /etc/systemd/system/rageadmin.service
```

Use:

```ini
[Unit]
Description=RageAdmin
After=network.target mariadb.service

[Service]
Type=simple
User=hpm
Group=hpm
WorkingDirectory=/opt/rageadmin/app
Environment=PANEL_PORT=20000
ExecStart=/opt/rageadmin/app/.venv/bin/python /opt/rageadmin/app/main.py --port 20000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rageadmin
sudo systemctl status rageadmin
```

View logs:

```bash
journalctl -u rageadmin -f
```

</details>

<details>
<summary><strong>Nginx reverse proxy example</strong></summary>

<br />

Install Nginx if needed:

```bash
sudo apt install -y nginx
```

Create a site:

```bash
sudo nano /etc/nginx/sites-available/rageadmin
```

Example config:

```nginx
server {
    listen 80;
    server_name panel.example.com;

    location / {
        proxy_pass http://127.0.0.1:20000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/rageadmin /etc/nginx/sites-enabled/rageadmin
sudo nginx -t
sudo systemctl reload nginx
```

If you use HTTPS behind a reverse proxy, these environment variables may be useful:

```bash
PANEL_FORCE_HTTPS=true
PANEL_SESSION_COOKIE_SECURE=true
```

</details>

## Updating

The built-in update system can check:

- The panel itself
- [RageMP](https://rage.mp/) server files

By default, panel update metadata is read from:

- [`update_config.json`](./update_config.json)

RageMP server updates are checked against the official Linux archive URL using HTTP metadata such as `ETag` and `Last-Modified`.

You can also trigger update checks directly from the web panel.

## Security Notes

- Use a strong admin password
- Do not expose the panel publicly without proper firewall or reverse proxy rules
- Use HTTPS when exposing the panel to the internet
- Protect the generated panel secret
- Give only the permissions needed to non-admin users

## Troubleshooting

### The panel does not open

Check that the service is running and the port is open:

```bash
ss -tulpn | grep 20000
```

### Setup cannot connect to the database

Make sure:

- MariaDB / MySQL is running
- The database already exists
- The username, password, host, and port are correct
- The DB user has privileges on the selected database

Quick check:

```bash
mysql -h 127.0.0.1 -u rageadmin -p rageadmin
```

### The server executable is missing

The panel expects the Linux server binary at:

```text
./RageMP-Server/ragemp-srv/ragemp-server
```

If setup did not finish correctly, start the panel again and complete the setup flow.

### WebSocket or live console issues behind Nginx

Make sure your reverse proxy passes:

- `Upgrade`
- `Connection`

The Nginx example above already includes both.

### Running on Windows or ARM

That is not supported for this project.

## Notes

This repository is focused on [RageMP](https://rage.mp/) server management on Linux.
If you want the easiest deployment path and already use Pterodactyl, use the included egg and keep the panel behind proper network rules.
