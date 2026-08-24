# AquaVision

AquaVision is an offline-first visual water monitoring platform running on a Raspberry Pi 5.

## Current system

- Raspberry Pi 5
- DHT11 temperature and humidity monitoring
- I2C UV sensor monitoring
- OpenCV visual anomaly detection
- Visual affected-area percentage
- SQLite offline database
- Local dashboard
- Supabase synchronization
- Historical inspections and telemetry

## Architecture

Sensors / Camera
↓
Raspberry Pi
↓
SQLite
↓
Supabase when internet is available

AquaVision continues operating locally without internet.

## Security

Secrets and deployment credentials are stored using environment variables and are not committed to Git.