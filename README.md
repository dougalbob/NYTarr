**NYTarr** is a lightweight, self-hosted Docker application designed to automate the daily downloading of *The New York Times* newspaper (PDF format) from Usenet. It coordinates with **Prowlarr** for indexer searching and **SABnzbd** for downloading, then renames and organizes the files chronologically for your favorite media readers (like **Grimmory** or **Kavita**).

---

## Features

- 🕒 **Automatic Daily Searches:** Searches daily at a configurable hour (e.g., 12:00 PM UK time) for the current/previous day's paper.
- 🔄 **Smart Retry:** If today's paper is not found, the app retries automatically every *X* hours.
- 📅 **Robust Backfill:** Automatically checks for and grabs any missing papers within a 3-day window to cover delays or service downtime.
- 🏷️ **Auto-Rename & Clean:** Renames messy Usenet release titles to a clean, chronological structure:  
  `The.New.York.Times.-.14.September.2026` ➡️ `New York Times - 2026.09.14.pdf`
- 🖥️ **Simple Web UI:**
  - View download history and current statuses (`completed`, `downloading`, `searching`, `failed`).
  - Access settings directly from the web interface.
  - View real-time application logs.
  - Manual "Search Now" button to force an instant search/backfill.
- 🐳 **Docker-Native:** Tailored for easy deployment on Unraid or any Docker environment.

---

## App Directory Structure

Your project folder should look like this:

```text
NYTarr/
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── logs.html
│   └── settings.html
├── app.py
├── web.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md

Quick Start (Docker Compose)
Create your project directory on your host machine (e.g., /mnt/user/data/pythonscripts/NYTarr).
Save the files from this repository into that folder.
Start the container:
Bash

docker compose up -d --build
The web UI will be accessible at: http://<YOUR_SERVER_IP>:6810

Configuration
When the container runs for the first time, a default config.yml and grabberstate.json (to track history) will be created in your appdata folder (e.g., /mnt/user/appdata/nytarr/).

You can configure the settings directly via the Web UI Settings Tab, or manually edit /config/config.yml:

prowlarr:
  url: "http://192.168.1.241:9696"     # Prowlarr local URL
  api_key: "your_prowlarr_api_key"      # Prowlarr API Key
  indexer_name: "NZBFinder"             # Specify a single indexer (optional)

sabnzbd:
  url: "http://192.168.1.241:8090"      # SABnzbd local URL
  api_key: "your_sabnzbd_api_key"        # SABnzbd API Key (not the NZB key)
  category: "nytarr"                    # Category configured in SABnzbd

schedule:
  search_start_hour: 12                 # Hour of the day to start searching (0-23)
  search_interval_hours: 2              # Retry interval if the paper is not yet found

paths:
  download_folder: "/data/downloads/complete/nytarr" # Inside-container path where SAB saves downloads
  media_folder: "/data/media/new_york_times"          # Inside-container path where final PDFs go

backfill:
  days: 3                               # How many days back to look for missing editions

timezone: "Europe/London"               # Local timezone

Unraid Setup Integration
1. SABnzbd Configuration
To make sure downloads are routed correctly:

Open your SABnzbd interface.
Go to Config ➡️ Categories.
Add a new category:
Category: nytarr
Folder/Path: nytarr (This maps to /data/downloads/complete/nytarr inside your containers).
2. E-Reader / PDF Viewer Integration (e.g., Grimmory)
To read the newspapers on an e-reader tool like Grimmory:

Edit your Grimmory Docker template in Unraid.
Scroll to the bottom and click Add another Path, Port, Variable, Label or Device.
Set the following parameters:
Config Type: Path
Name: New York Times
Container Path: /news
Host Path: /mnt/user/data/media/new_york_times
Access Mode: Read/Only
Save and restart Grimmory, then add /news as a library inside the Grimmory UI.
License
This project is open-source and free to use. Modify and adapt to your needs!
EOF