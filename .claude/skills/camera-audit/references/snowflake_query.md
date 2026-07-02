# Building the input CSV from Snowflake

The audit CSV needs these six columns: `IP`, `LIVE_UNIT_SERIAL_NM`,
`LOCATION_NM`, `CLIENT_NM`, `MANUFACTURER`, `MODEL` (only `IP` is strictly
required by `run_audit.py`, but pulling all six from Snowflake up front avoids
a second lookup later).

```sql
SELECT
    nc.PUBLIC_IP AS IP,
    lu.LIVE_UNIT_SERIAL_NR AS LIVE_UNIT_SERIAL_NM,
    lu.LOCATION_NM,
    lu.CLIENT_NM,
    cm.MANUFACTURER,
    cm.MODEL
FROM STAGING.HORUS.NETWORK_CAMERAS nc
JOIN EDW.DM_PRODUCT.LIVE_UNIT lu
    ON nc.LIVE_UNIT_ID = lu.LIVE_UNIT_ID
LEFT JOIN STAGING.HORUS.CAMERA_MFTRS cm
    ON nc.CAMERA_MFTRS_ID = cm.ID
WHERE lu.live_unit_serial_nr IN ('TDC14016')
GROUP BY lu.CLIENT_NM, lu.LOCATION_NM, nc.PUBLIC_IP, lu.LIVE_UNIT_SERIAL_NR, cm.MANUFACTURER, cm.MODEL
ORDER BY lu.LOCATION_NM
```

Swap the `WHERE` clause for whatever filter fits the request (a list of unit
serials, a client name, a location, etc.) -- the shape of the output is what
matters, not this particular filter.

Export the result to CSV with the header row intact, matching column names
exactly (`IP` and `MANUFACTURER` in particular -- `run_audit.py` and
`camera_engine.classify_manufacturer()` match on those exact header names).
