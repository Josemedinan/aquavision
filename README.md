# AquaVision

AquaVision is an **offline-first visual water monitoring platform** running on a Raspberry Pi 5.

The system combines environmental sensors, computer vision, local data storage and cloud synchronization to continuously monitor a water tank while remaining operational without internet access.

## Current System

- Raspberry Pi 5
- DHT11 temperature and humidity monitoring
- I2C UV sensor monitoring
- OpenCV visual anomaly detection
- Visual affected-area percentage
- SQLite local database
- Local Flask dashboard
- Supabase / PostgreSQL cloud database
- Historical inspections and telemetry
- Offline-to-cloud synchronization

---

## Architecture

```text
        Sensors + Camera
               │
               ▼
        ┌──────────────┐
        │ Raspberry Pi │
        │  AquaVision  │
        └──────┬───────┘
               │
       ┌───────┴────────┐
       ▼                ▼
   OpenCV            SQLite
   Analysis          Local DB
                       │
              ┌────────┴────────┐
              ▼                 ▼
       Local Dashboard     Sync Worker
                                │
                         Internet available
                                │
                                ▼
                     Supabase / PostgreSQL
```

AquaVision stores data **locally first**. Internet connectivity is only required for cloud synchronization.

---

## Database Architecture

The database is a central part of AquaVision.

The platform uses two database layers:

### SQLite — Local Database

SQLite runs directly on the Raspberry Pi and stores:

- Temperature and humidity readings
- UV sensor readings
- Visual inspections
- Detection percentages
- Capture quality
- Alignment scores
- Historical measurements
- Synchronization status

```text
Sensors / Camera
       │
       ▼
   Raspberry Pi
       │
       ▼
     SQLite
       │
       ├── Local Dashboard
       └── Pending Cloud Sync
```

This allows AquaVision to continue collecting and analyzing data even without internet.

### Supabase — Cloud Database

When internet becomes available, pending records can be synchronized with a PostgreSQL database hosted on Supabase.

The relational structure follows:

```text
Organization
     │
     ▼
   Sites
     │
     ▼
   Tanks
     │
     ▼
  Devices
     │
 ┌───┼─────────────┐
 ▼   ▼             ▼
Sensors          Inspections
     │             │
     ▼             ▼
UV Readings     Images / Alerts
```

Main cloud tables include:

- `organizations`
- `organization_members`
- `profiles`
- `sites`
- `tanks`
- `devices`
- `sensor_readings`
- `uv_readings`
- `inspections`
- `inspection_images`
- `reference_images`
- `alerts`

---

## Offline-First Data Flow

```text
New Measurement
      │
      ▼
    SQLite
      │
      ├── No Internet ──► Keep Locally
      │
      └── Internet ─────► Supabase
                              │
                              ▼
                         PostgreSQL
```

Each synchronized record uses an `event_uuid` to help identify measurements consistently between the local and cloud databases.

---

## Visual Monitoring

AquaVision compares new images against a registered clean reference.

```text
Reference Image
      │
      ▼
New Inspection
      │
      ▼
Image Alignment
      │
      ▼
Water ROI
      │
      ▼
OpenCV Analysis
      │
      ▼
Affected Area %
      │
      ▼
SQLite History
```

The resulting percentage represents **visual change relative to the clean reference** and should not be interpreted as a chemical contamination measurement.

---

## Database Security

Supabase uses **Row Level Security (RLS)** to control access to cloud data.

The current database architecture supports:

- `owner`
- `admin`
- `operator`
- `viewer`

Users are associated with organizations, allowing data access to be separated between different organizations and installations.

Database definitions are version controlled in:

```text
database/
├── 01_schema.sql
├── 02_rls.sql
└── 03_seed.example.sql
```

---

## Repository Structure

```text
AquaVision/
│
├── src/
│   └── receptor.py
│
├── database/
│   ├── 01_schema.sql
│   ├── 02_rls.sql
│   └── 03_seed.example.sql
│
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Security

Production credentials are stored using environment variables.

```env
SUPABASE_URL=
SUPABASE_KEY=
ORGANIZATION_ID=
SITE_ID=
TANK_ID=
DEVICE_ID=
```

The real `.env` file, SQLite databases, local images and other runtime data are excluded from Git.

---

## Current Status

**Implemented**

- Raspberry Pi monitoring
- Environmental telemetry
- UV sensor communication
- Visual anomaly detection
- SQLite persistence
- Local dashboard
- Historical data
- Supabase database architecture
- Row Level Security
- Offline operation
- Cloud synchronization

**In development**

- UV sensor calibration
- Cloud image storage
- Remote dashboard
- Improved device authentication
- Additional water-quality sensors

---

## Core Principle

> **AquaVision must continue monitoring even when internet connectivity is unavailable.**

SQLite provides local operational persistence, while Supabase provides centralized relational storage, security and historical access.
