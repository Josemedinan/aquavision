-- ============================================================
-- AQUAVISION
-- ROW LEVEL SECURITY
-- ============================================================


-- ============================================================
-- ENABLE RLS
-- ============================================================

alter table public.organizations
enable row level security;

alter table public.profiles
enable row level security;

alter table public.organization_members
enable row level security;

alter table public.sites
enable row level security;

alter table public.tanks
enable row level security;

alter table public.devices
enable row level security;

alter table public.sensor_readings
enable row level security;

alter table public.uv_readings
enable row level security;

alter table public.inspections
enable row level security;

alter table public.inspection_images
enable row level security;

alter table public.reference_images
enable row level security;

alter table public.alerts
enable row level security;


-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

create or replace function public.is_org_member(
    org_id uuid
)
returns boolean
language sql
stable
security definer
set search_path = public
as $$

    select exists (

        select 1

        from public.organization_members om

        where om.organization_id = org_id

        and om.user_id = auth.uid()

    );

$$;


create or replace function public.has_org_role(
    org_id uuid,
    allowed_roles text[]
)
returns boolean
language sql
stable
security definer
set search_path = public
as $$

    select exists (

        select 1

        from public.organization_members om

        where om.organization_id = org_id

        and om.user_id = auth.uid()

        and om.role = any(allowed_roles)

    );

$$;


-- ============================================================
-- PROFILES
-- ============================================================

drop policy if exists
"users_read_own_profile"
on public.profiles;

create policy
"users_read_own_profile"

on public.profiles

for select

to authenticated

using (
    id = auth.uid()
);


drop policy if exists
"users_update_own_profile"
on public.profiles;

create policy
"users_update_own_profile"

on public.profiles

for update

to authenticated

using (
    id = auth.uid()
)

with check (
    id = auth.uid()
);


-- ============================================================
-- ORGANIZATION MEMBERS
-- ============================================================

drop policy if exists
"members_read_org_members"
on public.organization_members;

create policy
"members_read_org_members"

on public.organization_members

for select

to authenticated

using (
    public.is_org_member(
        organization_id
    )
);


drop policy if exists
"admins_insert_members"
on public.organization_members;

create policy
"admins_insert_members"

on public.organization_members

for insert

to authenticated

with check (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
);


drop policy if exists
"admins_update_members"
on public.organization_members;

create policy
"admins_update_members"

on public.organization_members

for update

to authenticated

using (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
)

with check (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
);


drop policy if exists
"admins_delete_members"
on public.organization_members;

create policy
"admins_delete_members"

on public.organization_members

for delete

to authenticated

using (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
);


-- ============================================================
-- ORGANIZATIONS
-- ============================================================

drop policy if exists
"members_read_organizations"
on public.organizations;

create policy
"members_read_organizations"

on public.organizations

for select

to authenticated

using (
    public.is_org_member(id)
);


drop policy if exists
"owners_update_organizations"
on public.organizations;

create policy
"owners_update_organizations"

on public.organizations

for update

to authenticated

using (
    public.has_org_role(
        id,
        array['owner']
    )
)

with check (
    public.has_org_role(
        id,
        array['owner']
    )
);


-- ============================================================
-- SITES
-- ============================================================

drop policy if exists
"members_read_sites"
on public.sites;

create policy
"members_read_sites"

on public.sites

for select

to authenticated

using (
    public.is_org_member(
        organization_id
    )
);


drop policy if exists
"admins_manage_sites"
on public.sites;

create policy
"admins_manage_sites"

on public.sites

for all

to authenticated

using (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
)

with check (
    public.has_org_role(
        organization_id,
        array[
            'owner',
            'admin'
        ]
    )
);


-- ============================================================
-- TANKS
-- ============================================================

drop policy if exists
"members_read_tanks"
on public.tanks;

create policy
"members_read_tanks"

on public.tanks

for select

to authenticated

using (

    exists (

        select 1

        from public.sites s

        where s.id = tanks.site_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


drop policy if exists
"admins_manage_tanks"
on public.tanks;

create policy
"admins_manage_tanks"

on public.tanks

for all

to authenticated

using (

    exists (

        select 1

        from public.sites s

        where s.id = tanks.site_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

)

with check (

    exists (

        select 1

        from public.sites s

        where s.id = tanks.site_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

);


-- ============================================================
-- DEVICES
-- ============================================================

drop policy if exists
"members_read_devices"
on public.devices;

create policy
"members_read_devices"

on public.devices

for select

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = devices.tank_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


drop policy if exists
"admins_manage_devices"
on public.devices;

create policy
"admins_manage_devices"

on public.devices

for all

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = devices.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

)

with check (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = devices.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

);


-- ============================================================
-- SENSOR READINGS
-- ============================================================

drop policy if exists
"members_read_sensor_readings"
on public.sensor_readings;

create policy
"members_read_sensor_readings"

on public.sensor_readings

for select

to authenticated

using (

    exists (

        select 1

        from public.devices d

        join public.tanks t
        on t.id = d.tank_id

        join public.sites s
        on s.id = t.site_id

        where d.id = sensor_readings.device_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


-- ============================================================
-- UV READINGS
-- ============================================================

drop policy if exists
"members_read_uv_readings"
on public.uv_readings;

create policy
"members_read_uv_readings"

on public.uv_readings

for select

to authenticated

using (

    exists (

        select 1

        from public.devices d

        join public.tanks t
        on t.id = d.tank_id

        join public.sites s
        on s.id = t.site_id

        where d.id = uv_readings.device_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


-- ============================================================
-- INSPECTIONS
-- ============================================================

drop policy if exists
"members_read_inspections"
on public.inspections;

create policy
"members_read_inspections"

on public.inspections

for select

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = inspections.tank_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


-- ============================================================
-- INSPECTION IMAGES
-- ============================================================

drop policy if exists
"members_read_inspection_images"
on public.inspection_images;

create policy
"members_read_inspection_images"

on public.inspection_images

for select

to authenticated

using (

    exists (

        select 1

        from public.inspections i

        join public.tanks t
        on t.id = i.tank_id

        join public.sites s
        on s.id = t.site_id

        where i.id =
            inspection_images.inspection_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


-- ============================================================
-- REFERENCE IMAGES
-- ============================================================

drop policy if exists
"members_read_reference_images"
on public.reference_images;

create policy
"members_read_reference_images"

on public.reference_images

for select

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id =
            reference_images.tank_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


drop policy if exists
"admins_manage_reference_images"
on public.reference_images;

create policy
"admins_manage_reference_images"

on public.reference_images

for all

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id =
            reference_images.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

)

with check (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id =
            reference_images.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin'
            ]
        )

    )

);


-- ============================================================
-- ALERTS
-- ============================================================

drop policy if exists
"members_read_alerts"
on public.alerts;

create policy
"members_read_alerts"

on public.alerts

for select

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = alerts.tank_id

        and public.is_org_member(
            s.organization_id
        )

    )

);


drop policy if exists
"operators_update_alerts"
on public.alerts;

create policy
"operators_update_alerts"

on public.alerts

for update

to authenticated

using (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = alerts.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin',
                'operator'
            ]
        )

    )

)

with check (

    exists (

        select 1

        from public.tanks t

        join public.sites s
        on s.id = t.site_id

        where t.id = alerts.tank_id

        and public.has_org_role(
            s.organization_id,
            array[
                'owner',
                'admin',
                'operator'
            ]
        )

    )

);


-- ============================================================
-- RASPBERRY PI INSERT PERMISSIONS
--
-- IMPORTANT:
-- Replace DEVICE_UUID_HERE and TANK_UUID_HERE
-- only in the private Supabase deployment.
--
-- Do NOT commit real device UUIDs here.
-- ============================================================

grant insert
on public.sensor_readings
to anon;

grant insert
on public.uv_readings
to anon;

grant insert
on public.inspections
to anon;


grant usage, select
on sequence public.sensor_readings_id_seq
to anon;

grant usage, select
on sequence public.uv_readings_id_seq
to anon;


drop policy if exists
"raspberry_insert_sensor"
on public.sensor_readings;

create policy
"raspberry_insert_sensor"

on public.sensor_readings

for insert

to anon

with check (
    device_id =
    'DEVICE_UUID_HERE'::uuid
);


drop policy if exists
"raspberry_insert_uv"
on public.uv_readings;

create policy
"raspberry_insert_uv"

on public.uv_readings

for insert

to anon

with check (
    device_id =
    'DEVICE_UUID_HERE'::uuid
);


drop policy if exists
"raspberry_insert_inspections"
on public.inspections;

create policy
"raspberry_insert_inspections"

on public.inspections

for insert

to anon

with check (

    device_id =
    'DEVICE_UUID_HERE'::uuid

    and

    tank_id =
    'TANK_UUID_HERE'::uuid

);