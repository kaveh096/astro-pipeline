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

## Status: SPCC works, and is the default

**SPCC requires Siril >= 1.4.4.** On 1.4.3 it crashed the process outright
with an access violation (`0xC0000005`), always at
`Applying aperture photometry to 73 stars`, regardless of catalogue source
or sensor/filter configuration. Upgrading to 1.4.4 fixed it -- despite
1.4.4's changelog mentioning nothing about SPCC, photometry, or colour
calibration. Three hypotheses about the crash were tested against 1.4.3
and all three were wrong (it was not the online-catalogue fallback, and
not the unset sensor/filters); the answer was simply the version.

Verified working on the real M51/T24 data:

```
SPCC will use mono senor "KAF16803" and filters "Astrodon Red (E series)", ...
Getting stars from local catalogue Gaia DR3 xp_sampled for SPCC
Applying aperture photometry to 73 stars.
30 stars excluded from the calculation
Found a solution for color calibration using 43 stars.
K0: 0.925  K1: 0.899  K2: 1.000
Spectrophotometric Color Calibration succeeded.
```

`colour_calibration="spcc"` is the pipeline default. PCC remains available
(`colour_calibration="pcc"`) for comparison, but has no advantage here: it
depends on VizieR at runtime, which is exactly what broke.

### Expect far fewer stars than PCC

SPCC used **43** stars where PCC used **246** on the same data. That is
normal, not a coverage gap -- SPCC can only use stars that have Gaia XP
*sampled spectra*, a much smaller population than plain broadband
photometry. Checked explicitly for this field: it lies entirely within
chunk 10 even out to a 3-degree radius, so nothing is missing.

To rule out a genuine boundary problem on a new target, cone-search the
chunks the field actually touches:

```python
from astropy_healpix import HEALPix
import astropy.units as u
hp = HEALPix(nside=2, order="nested")
sorted(int(c) for c in hp.cone_search_lonlat(ra*u.deg, dec*u.deg, 1.5*u.deg))
```

If that returns more than one chunk, download them all.

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
