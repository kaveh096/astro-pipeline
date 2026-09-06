# Colour calibration: catalogues, and the per-project sky region

How white balancing gets its reference data, what to download per project,
and the current state of SPCC vs PCC.

## The intended workflow

For each new project, download only the sky region you are imaging, then
calibrate against it locally. No dependency on a third-party server at
processing time.

The catalogues come from Gaia DR3, repackaged for Siril and hosted on
Zenodo. There are **two different catalogues** and they are not
interchangeable:

| | used by | form | size |
|---|---|---|---|
| **Astrometric** ([Zenodo 14692304](https://zenodo.org/records/14692304)) | `pcc -catalog=localgaia` | one whole-sky file | **1.1 GB** compressed |
| **Spectrophotometric** ([Zenodo 14738271](https://zenodo.org/records/14738271)) | `spcc` | 48 files, chunked by sky region | **10.6 GB** total, but you fetch only your chunks |

Only the spectrophotometric one is regional. The astrometric one is a
single whole-sky file, so "download just my region" does not apply to it.

## Finding your chunk

The 48 spectrophotometric files are level-1 HEALPix pixels (48 = 12 x 4^1),
nested ordering, each covering ~859 square degrees. `astropy_healpix` is
already a dependency:

```python
from astropy_healpix import HEALPix
import astropy.units as u
hp = HEALPix(nside=2, order="nested")
chunk = int(hp.lonlat_to_healpix(ra_deg * u.deg, dec_deg * u.deg))
```

Worked examples:

| target | RA | Dec | chunk | file size |
|---|---|---|---|---|
| M51 | 202.470 | +47.195 | **10** | 49.4 MB |
| M31 | 10.685 | +41.269 | 2 | |
| M13 | 250.423 | +36.461 | 9 | 101.6 MB |
| M42 | 83.822 | -5.391 | 20 | |

A target near a chunk boundary, or a wide mosaic, may need the neighbouring
chunk as well.

## Installing a chunk

Siril expects a **directory**, despite the `.dat` name — the default
setting `~/.local/share/siril/gaia_photometric.dat` is a folder that
Siril iterates, not a file. Getting this wrong produces
"directory iterator cannot open directory".

```
curl -L -o chunk10.dat.bz2 \
  "https://zenodo.org/records/14738271/files/siril_cat1_healpix8_xpsamp_10.dat.bz2?download=1"
bunzip2 chunk10.dat.bz2
mkdir -p "%LOCALAPPDATA%/siril/gaia_photometric.dat"
mv chunk10.dat "%LOCALAPPDATA%/siril/gaia_photometric.dat/siril_cat1_healpix8_xpsamp_10.dat"
```

Then point Siril at it (persists in config, so this is one-time):

```
set core.catalogue_gaia_photo=C:/Users/<you>/AppData/Local/siril/gaia_photometric.dat
```

Chunk 10 is installed on this machine and Siril reads it — the log line
`Getting stars from local catalogue Gaia DR3 xp_sampled for SPCC`
confirms it, so the install procedure above is verified working.

## Sensor and filter definitions

SPCC also needs to know the sensor and filters, to model spectral
response. Those live in a separate repository that Siril syncs from its
GUI (`gui.use_spcc_repository`); it is **not** fetched in headless CLI
use. Clone it manually:

```
git clone --depth 1 https://gitlab.com/free-astro/siril-spcc-database.git \
  "%LOCALAPPDATA%/siril/siril-spcc-database"
```

Names must match the JSON `name` field exactly, and contain spaces, so
they need quoting in a `.ssf` script. For T24 (Apogee, 9 um pixels,
4096x4096 -> KAF-16803, Astrodon filters):

```
spcc -catalog=localgaia "-monosensor=KAF16803" "-rfilter=Astrodon Red (E series)" \
     "-gfilter=Astrodon Green (E series)" "-bfilter=Astrodon Blue (E / I series)"
```

## Current status: SPCC does not work here

**SPCC crashes deterministically** on this setup — Siril 1.4.3, Windows —
with an access violation (`0xC0000005`), always at the same point:
`Applying aperture photometry to 73 stars`.

Three hypotheses were tested and all three were wrong:

1. *"It falls back to the online catalogue and that path is broken."*
   Wrong — with the local catalogue installed and demonstrably in use, it
   still crashes.
2. *"Sensor and filters are unset (`(NULL)`), so it dereferences null."*
   Wrong — with `KAF16803` and the Astrodon filters resolving correctly
   in the log, it still crashes.
3. Whatever remains is inside Siril's SPCC implementation. The crash is
   independent of catalogue source and of sensor/filter configuration.

PCC on comparable data reaches "Applying aperture photometry to **652**
stars" and completes; SPCC consistently finds 73 and dies there.

So **PCC remains the only working colour calibration path** for now,
contrary to the plan of dropping it. Worth trying:

- Siril 1.5.0, where SPCC has seen further development.
- Reporting upstream, with the reproducer above.

## PCC and the VizieR dependency

PCC defaults to NOMAD over the network, which broke mid-session when
VizieR returned HTTP 403 for several hours. `pcc -catalog=localgaia`
avoids that, but wants the **astrometric** catalogue (1.1 GB) — the
spectrophotometric chunks do not satisfy it:

```
Local Gaia catalog is unavailable, reverting to online Gaia catalog via Vizier
```

So making PCC fully offline costs the 1.1 GB download, and is
independent of anything SPCC needs.
