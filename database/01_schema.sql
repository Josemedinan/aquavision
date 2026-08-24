

create extension if not exists pgcrypto;


-- ============================================================
-- ORGANIZATIONS
-- ============================================================

create table if not exists public.organizations (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    description text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- PROFILES
-- ============================================================

create table if not exists public.profiles (
    id uuid primary key
        references auth.users(id)
        on delete cascade,

    full_name text,
    email text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- ORGANIZATION MEMBERS
-- ============================================================

create table if not exists public.organization_members (
    organization_id uuid not null
        references public.organizations(id)
        on delete cascade,

    user_id uuid not null
        references auth.users(id)
        on delete cascade,

    role text not null
        check (
            role in (
                'owner',
                'admin',
                'operator',
                'viewer'
            )
        ),

    created_at timestamptz not null default now(),

    primary key (
        organization_id,
        user_id
    )
);


-- ============================================================
-- SITES
-- ============================================================

create table if not exists public.sites (
    id uuid primary key default gen_random_uuid(),

    organization_id uuid not null
        references public.organizations(id)
        on delete cascade,

    name text not null,
    description text,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- TANKS
-- ============================================================

create table if not exists public.tanks (
    id uuid primary key default gen_random_uuid(),

    site_id uuid not null
        references public.sites(id)
        on delete cascade,

    name text not null,
    description text,

    capacity_liters numeric,

    active boolean not null default true,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- DEVICES
-- ============================================================

create table if not exists public.devices (
    id uuid primary key default gen_random_uuid(),

    tank_id uuid not null
        references public.tanks(id)
        on delete cascade,

    device_code text not null unique,

    name text,
    model text,
    software_version text,

    active boolean not null default true,

    last_seen timestamptz,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);


-- ============================================================
-- SENSOR READINGS
-- DHT11
-- ============================================================

create table if not exists public.sensor_readings (
    id bigint generated always as identity primary key,

    event_uuid uuid not null unique,

    device_id uuid not null
        references public.devices(id)
        on delete cascade,

    temperature_c real,
    humidity_pct real,

    measured_at timestamptz not null,

    received_at timestamptz not null default now()
);


-- ============================================================
-- UV READINGS
-- ============================================================

create table if not exists public.uv_readings (
    id bigint generated always as identity primary key,

    event_uuid uuid not null unique,

    device_id uuid not null
        references public.devices(id)
        on delete cascade,

    raw_register_00 integer,
    raw_register_01 integer,
    raw_register_02 integer,
    raw_register_11 integer,
    raw_register_3a integer,

    raw_word_00 integer,
    raw_word_10 integer,

    calibrated_uv_index real,
    calibration_version text,

    measured_at timestamptz not null,

    received_at timestamptz not null default now()
);


-- ============================================================
-- INSPECTIONS
-- VISUAL DIRT / ANOMALY DETECTION
-- ============================================================

create table if not exists public.inspections (
    id uuid primary key default gen_random_uuid(),

    event_uuid uuid not null unique,

    tank_id uuid not null
        references public.tanks(id)
        on delete cascade,

    device_id uuid
        references public.devices(id)
        on delete set null,

    raw_percentage real,
    baseline_noise real,
    corrected_percentage real,

    condition text
        check (
            condition in (
                'LIMPIO',
                'OBSERVACION',
                'SUCIO',
                'CRITICO'
            )
        ),

    capture_quality text,
    quality_score real,
    alignment_score real,

    detection_version text,

    captured_at timestamptz not null,

    received_at timestamptz not null default now()
);


-- ============================================================
-- INSPECTION IMAGES
-- ============================================================

create table if not exists public.inspection_images (
    id uuid primary key default gen_random_uuid(),

    inspection_id uuid not null
        references public.inspections(id)
        on delete cascade,

    image_type text not null
        check (
            image_type in (
                'original',
                'overlay',
                'processed'
            )
        ),

    storage_path text not null,

    created_at timestamptz not null default now()
);


-- ============================================================
-- REFERENCE IMAGES
-- ============================================================

create table if not exists public.reference_images (
    id uuid primary key default gen_random_uuid(),

    tank_id uuid not null
        references public.tanks(id)
        on delete cascade,

    storage_path text not null,

    active boolean not null default true,

    baseline_noise real,

    created_at timestamptz not null default now()
);


-- ============================================================
-- ALERTS
-- ============================================================

create table if not exists public.alerts (
    id uuid primary key default gen_random_uuid(),

    tank_id uuid not null
        references public.tanks(id)
        on delete cascade,

    inspection_id uuid
        references public.inspections(id)
        on delete set null,

    alert_type text not null,

    severity text not null
        check (
            severity in (
                'info',
                'warning',
                'critical'
            )
        ),

    title text not null,

    message text,

    acknowledged boolean not null default false,

    acknowledged_by uuid
        references auth.users(id)
        on delete set null,

    acknowledged_at timestamptz,

    created_at timestamptz not null default now()
);


-- ============================================================
-- INDEXES
-- ============================================================

create index if not exists idx_sites_organization
on public.sites(organization_id);


create index if not exists idx_members_user
on public.organization_members(user_id);


create index if not exists idx_tanks_site
on public.tanks(site_id);


create index if not exists idx_devices_tank
on public.devices(tank_id);


create index if not exists idx_sensor_device_time
on public.sensor_readings(
    device_id,
    measured_at desc
);


create index if not exists idx_uv_device_time
on public.uv_readings(
    device_id,
    measured_at desc
);


create index if not exists idx_inspections_tank_time
on public.inspections(
    tank_id,
    captured_at desc
);


create index if not exists idx_inspections_device
on public.inspections(device_id);


create index if not exists idx_alerts_tank_time
on public.alerts(
    tank_id,
    created_at desc
);