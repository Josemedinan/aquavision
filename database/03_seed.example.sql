-- ============================================================
-- AQUAVISION
-- EXAMPLE INITIAL DATA
-- ============================================================

-- ORGANIZATION
insert into public.organizations (
    name,
    description
)
values (
    'AquaVision',
    'Main AquaVision organization'
)
returning id;


-- SITE
-- Replace ORGANIZATION_UUID_HERE
insert into public.sites (
    organization_id,
    name,
    description
)
values (
    'ORGANIZATION_UUID_HERE'::uuid,
    'Main Site',
    'Main AquaVision installation'
)
returning id;


-- TANK
-- Replace SITE_UUID_HERE
insert into public.tanks (
    site_id,
    name,
    description
)
values (
    'SITE_UUID_HERE'::uuid,
    'Tank 01',
    'Tank monitored by AquaVision'
)
returning id;


-- DEVICE
-- Replace TANK_UUID_HERE
insert into public.devices (
    tank_id,
    device_code,
    name,
    model,
    software_version
)
values (
    'TANK_UUID_HERE'::uuid,
    'AQUA-001',
    'AquaVision Raspberry 01',
    'Raspberry Pi 5',
    '7.1'
)
returning id;